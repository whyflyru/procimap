############################################################################
#    Copyright (C) 2008 by Michael Goerz                                   #
#    http://www.physik.fu-berlin.de/~goerz                                 #
#                                                                          #
#    This program is free software; you can redistribute it and#or modify  #
#    it under the terms of the GNU General Public License as published by  #
#    the Free Software Foundation; either version 3 of the License, or     #
#    (at your option) any later version.                                   #
#                                                                          #
#    This program is distributed in the hope that it will be useful,       #
#    but WITHOUT ANY WARRANTY; without even the implied warranty of        #
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the         #
#    GNU General Public License for more details.                          #
#                                                                          #
#    You should have received a copy of the GNU General Public License     #
#    along with this program; if not, write to the                         #
#    Free Software Foundation, Inc.,                                       #
#    59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.             #
############################################################################

""" This module contains functions that make it easier to work with Gmail.
    Identity is established by message-id and size
"""
from ProcImap.Utils.Processing import references_from_header
from ProcImap.ImapMailbox import ImapMailbox

import hashlib
import cPickle
import re
import mailbox as Mailbox # I'm already using 'mailbox' as a variable name
import email

ATTEMPTS = 10  # number of times the connection to the server will be renewed
               # if an exception occurs before the program gives up and passes
               # the exception on.


class DeleteFromTrashError(Exception):
    """ Raised if a message cannot be removed from the Gmail Trash folder 
        for some reason 
    """
    pass

class GmailCache:
    """ Class for keeping track of all the messages and their 
        relationships on a Gmail account 
    """
    local_uid_pattern = re.compile(r'(?P<mailbox>.*)\.(?P<uid>[0-9]+)')
    messageid_pattern = re.compile(r'<(?P<left>[^@]+)@(?P<right>[^@+])>')

    def __init__(self, server, autosave=None):
        """ Initialize cache for the given server """
        self._mb = ImapMailbox((server, 'INBOX'))
        # sha244hash.size => {[local_uids], message_id, [references]}
        self.data_for_hash_id    = {} 
        # mailboxname.UID => sha244hash.size
        self.hash_id_for_local_uid  = {} 
        # message-id => set([sha244hash.size, ..])
        self.hash_ids_for_message_id = {} 
        self.unknown_references = {}
        self.update_results = {}
        self.mailboxnames = self._mb.server.list()
        self._attempts = 0
        self.autosave = autosave

    def clear(self):
        """ Discard all cached data """
        self.data_for_hash_id    = {}
        self.hash_id_for_local_uid  = {}
        self.hash_ids_for_message_id = {}
    
    def update(self, ignore=None):
        """ Update the cache """
        if ignore is None:
            ignore = ['[Gmail]/Trash]', '[Gmail]/Spam']
        try:
            self._mb = ImapMailbox((self._mb.server.clone(), 'INBOX'))
            self.mailboxnames = self._mb.server.list()
            # TODO: handle delete mailboxes
            for mailboxname in self.mailboxnames:
                if mailboxname in ignore:
                    continue
                print ("Processing Mailbox %s" % mailboxname)
                self._mb.switch(mailboxname)
                uids_on_server = self._mb.get_all_uids()
                self._remove_deleted_data(mailboxname, uids_on_server)
                self._add_new_data(mailboxname, uids_on_server)
                self._autosave()
                print("  Done.")
        except Exception, data:
            # reconnect
            self._autosave()
            self._attempts += 1
            print "Exception occured: %s (attempt %s). Reconnect." \
                % (data, self._attempts)
            if self._attempts > ATTEMPTS:
                self._attempts = 0
                raise
            self._mb = ImapMailbox((self._mb.server.clone(), 'INBOX'))
            self.update()
        self._attempts = 0
        self._mb.close()

    def _remove_deleted_data(self, mailboxname, uids_on_server):
        """ 
        Given a list of uids on the server in the specified mailbox,
        remove the data of mails that do not occur in the uids_on_server
        list
        """
        print ("  Removing mails that were deleted on the server ...")
        # find local_uids in the current mailbox which are in the
        # cache but not on the server anymore
        local_uids_to_delete = [] 
        local_mailbox_uids = self.local_mailbox_uids(mailboxname)
        local_mailbox_uids.sort(key = lambda x: int(x.split('.')[-1]))
        local_uid_iterator = iter(local_mailbox_uids)
        try:
            for uid_on_server in uids_on_server:
                local_uid = local_uid_iterator.next()
                while not local_uid.endswith(str(uid_on_server)):
                    local_uids_to_delete.append(local_uid)
                    local_uid = local_uid_iterator.next()
        except StopIteration:
            pass
        # For all the mails found above, remove the data from
        # self.hash_id_for_local_uid, self.hash_uids, and 
        # self.hash_ids_for_message_id
        for local_uid in local_uids_to_delete:
            print "    Removing %s from cache" % local_uid
            hash_id = self.hash_id_for_local_uid[local_uid]
            message_id = self.data_for_hash_id[hash_id]['message_id']
            # We first break the link between the local_uid and 
            # the hash_id
            del self.hash_id_for_local_uid[local_uid]
            self.data_for_hash_id[hash_id]['local_uids'].remove(local_uid)
            # If there were several copies of the same mail in different
            # mailboxes, and we deleted only one instance of the message,
            # we don't have to do anything else. If, however, this was the
            # last instantiation of the message, we need to remove all the data
            # associated with it.
            if len(self.data_for_hash_id[hash_id]['local_uids']) == 0:
                references = self.data_for_hash_id[hash_id]['references']
                del self.data_for_hash_id[hash_id]
                # Remove the connection between the message_id and the hash_id
                self.hash_ids_for_message_id[message_id].remove(hash_id)
                if len(self.hash_ids_for_message_id[message_id]) == 0:
                    del self.hash_ids_for_message_id[message_id]
                # take care of the references as well: if the deleted message
                # wasn't in any thread, we don't have to do anything, but if
                # it was part of a thread, we still have to keep that
                # information
                if len(references) > 1:
                    self.unknown_references[message_id] = references
        print ("  Done.")

    def _add_new_data(self, mailboxname, uids_on_server):
        """
        Given a list of uids on the server in the specified mailbox,
        incorporate the data from all the mails specified in the 
        uids_on_server list.
        """
        self._mb.switch(mailboxname)
        print ("  Processing existing/new mails on server")
        iteration = 0

        for uid in uids_on_server:

            local_uid = "%s.%s" % (mailboxname, uid)

            print "    Processing %s" % local_uid
            if self.hash_id_for_local_uid.has_key(local_uid):
                # skip mails that are already in the cache
                continue
            iteration += 1
            if iteration == 1000:
                # Autosave every 1000 new mails
                iteration = 0
                self._autosave()

            header = self._mb.get_header(uid)
            sha244hash = hashlib.sha224(header.as_string()).hexdigest()
            size = self._mb.get_size(uid)
            hash_id = "%s.%s" % (sha244hash, size)
            message_id = header['message-id']

            # put into self.hash_ids_for_message_id
            if message_id is None:
                print("%s has no message-id!" % local_uid)
                print("You are strongly advised to give each message "
                        "a unique message-id")
            else:
                if self.hash_ids_for_message_id.has_key(message_id):
                    self.hash_ids_for_message_id[message_id].add(hash_id)
                    if len(self.hash_ids_for_message_id[message_id]) > 1:
                        print("WARNING: You have different messages "
                                "with the same message-id. This is "
                                "pretty bad. Try to fix your message-ids")
                else:
                    self.hash_ids_for_message_id[message_id] = set([hash_id])

            # put into self.data_for_hash_id
            self._collect_data_for_message(local_uid, hash_id, 
                                           message_id, header)

            # put into self.hash_id_for_local_uid
            self.hash_id_for_local_uid[local_uid] = hash_id

    def _collect_data_for_message(self, local_uid, hash_id, message_id, header):
        """
        Fill self.data_for_hash_id for the message specified by local_uid, 
        hash_id, and message_id, and that has the specified header.
        """
        if self.data_for_hash_id.has_key(hash_id):
            # The mail is the same one as we encountered before in another
            # mailbox, so all we have to do is to register it as an alias.
            self.data_for_hash_id[hash_id]['local_uids'].append(local_uid)
        else:
            # If the mail is new, we have to add all the data.
            # First, we find out about the thread (i.e. the refences)
            refs_in_mail = references_from_header(header)
            thread = [message_id]
            # We start with the thread of the new message that's already
            # in the system, either because the new message_id is in the
            # unknown_references, or because a mail referenced in the new
            # message is alreay pointing to a thread.
            if message_id in self.unknown_references.keys():
                thread += self.unknown_references[message_id]
                # Since we're dealing with the message right now, it's no
                # longer unknown
                del self.unknown_references[message_id] 
            else:
                for ref in refs_in_mail:
                    if self.hash_ids_for_message_id.has_key(ref):
                        known_hash_id = self.hash_ids_for_message_id[ref].pop()
                        self.hash_ids_for_message_id[ref].add(known_hash_id)
                        thread += \
                            self.data_for_hash_id[known_hash_id]['references']
                        break
            # Now that we have the existing thread (or an empty one), we extend
            # it with the references found in the new message.
            thread = list(set(thread).union(set(refs_in_mail)))
            # Lastly, we add the new thread to every message that's part of the
            # new thread, both  to known and unknown
            self.data_for_hash_id[hash_id] = { 
                'local_uids' : [local_uid],
                'message_id' : message_id,
                'references' : thread }
            for ref in thread:
                if self.hash_ids_for_message_id.has_key(ref):
                    for hash_id in self.hash_ids_for_message_id[ref]:
                        self.data_for_hash_id[hash_id]['references'] = thread
                else:
                    self.unknown_references[ref] = thread

    def local_mailbox_uids(self, mailboxname):
        """ Return a sorted list of all the keys stored in
            self.hash_id_for_local_uid that belong to the mailbox with
            mailboxname. E.g. a call to local_mailbox_uids('[Gmail]/All Mail')
            might return
            ['[Gmail]/All Mail.4', '[Gmail]/All Mail.10', 
             '[Gmail]/All Mail.12']
            if there are three message messages in that mailbox, with the
            UIDs 4, 10, and 12.
        """
        result = [luid for luid in self.hash_id_for_local_uid.keys() 
                  if (GmailCache.local_uid_pattern.match(luid).group('mailbox')
                       == mailboxname)]
        result.sort(key = lambda x: int(x.split('.')[-1]))
        return result

    def save(self, picklefile):
        """ Pickle the cache """
        data = { 'data_for_hash_id' : self.data_for_hash_id,
                 'hash_id_for_local_uid' : self.hash_id_for_local_uid,
                 'hash_ids_for_message_id' : self.hash_ids_for_message_id,
                 'unknown_references' : self.unknown_references}
        output = open(picklefile, 'wb')
        cPickle.dump(data, output, protocol=2)
        output.close()

    def _autosave(self):
        """ Save self to the file who's filename is given in self.autosave """
        if self.autosave is not None:
            print "Auto-save to %s" % self.autosave
            self.save(self.autosave)

    def load(self, picklefile):
        """ Load pickled cache """
        try:
            input = open(picklefile, 'rb')
            data = cPickle.load(input)
            input.close()
            self.data_for_hash_id = data['data_for_hash_id']
            self.hash_id_for_local_uid = data['hash_id_for_local_uid']
            self.hash_ids_for_message_id = data['hash_ids_for_message_id']
            self.unknown_references = data['unknown_references']
        except (IOError, EOFError), exc_message:
            print("Could not read data from %s: %s" % (picklefile, exc_message))
            print("No cached data read")

    def get_labels(self, local_uid):
        """ Return the list of labels (i.e. list of mailboxes) that the message
            with the given local_uid is in.
        """
        result = set()
        for luid in self.data_for_hash_id[ 
            self.hash_id_for_local_uid[local_uid] 
        ]['local_uids']:
            result.add(
                GmailCache.local_uid_pattern.match(luid).group('mailbox'))
        return list(result)

    def get_thread(self, key, mailbox=None):
        """ Return the message thread (based on Message-IDs) associated with
            the message described by the given key.

            The 'key' variable may either be a local_uid or a Message-ID. 

            If it is a local_uid, the result will be a list of local_uids in
            the same mailbox if no 'mailbox' is specified, or a list of
            local_uids in the mailbox specified as 'mailbox'. Note that the
            resulting list may not actually reflect the full thread, as it will
            not contain messages that are in other mailboxes. To get the full
            thread, you should set 'mailbox' to '[Gmail]/All Mail'.

            If 'key' is a Message-ID and 'mailbox' is not specified, the result
            will be a list of Message-IDs that belong to the same thread.
            If 'key' is a Message-ID and 'mailbox' is set to the name of
            an mailbox, a list of local_uids of messages belonging to the thread
            and residing in the specified mailbox will be returned.

            For example, suppose there are three messages in your Gmail account
            forming a thread: A first one in your Inbox (local_uid: INBOX.120)
            with Message-ID '<abc1@foobar>', a second one in your Sent folder
            (local_uid: [Gmail]/Sent.100) with Message-ID '<abc2@foobar>' and a
            third one in your Inbox (local_uid: INBOX.121) with Message-ID
            '<abc3@foobar>. Of course, these messages also apper in your 
            'All Mail' folder with the local_uid '[Gmail]/All Mail.500',
            '[Gmail]/All Mail.501', and '[Gmail]/All Mail.502' the following
            example calls would apply:

            >>> c.get_thread('INBOX.120')
            ['INBOX.120, INBOX.121]

            >>> c.get_thread('INBOX.120', mailbox='[Gmail]/All Mail')
            ['[Gmail]/All Mail.500', '[Gmail]/All Mail.501', 
             '[Gmail]/All Mail.502']

            >>> c.get_thread('<abc1@foobar>')
            ['<abc1@foobar>', '<abc2@foobar>', '<abc3@foobar>']

            >>> c.get_thread('<abc2@foobar>', mailbox='INBOX')
            ['INBOX.120, INBOX.121]
        """
        raise NotImplementedError

    def backup(self, target, uids=None, update=True):
        """ backup the selfd Gmail account to a target mailbox 

            self must be an instance of Gmailself, target must be a string
            pointing to a local mbox file.

            All the emails stored on the Gmail account are appended to the
            target mailbox, the headers of each mail has
            'X-ProcImap-GmailLabel' header fields added, which contains a list
            of all the mailboxes that have a copy of this mail. Additionally,
            there will be an 'X-ProcImap-GmailUID' header field that contains
            the message's original UID in '[Gmail] All Mail'.

            Return a list of uid's of emails that failed to copy. These uid's
            are valid in the '[Gmail]/All Mail' mailbox on the server.

            If the optional 'uids' argument is given, it has to be a list of
            uid's.  Only the uid's in that list are backed up. The uid's must
            be valid in the '[Gmail]/All Mail' mailbox. This is intended for
            completing a previous backup.

            Before backup, the cache will be updated, unless you specify 
            'update' as False.
        """
        if not isinstance(target, basestring):
            raise TypeError, "target must be a string"
        if update:
            self.update()
        self._mb = ImapMailbox((self._mb.server.clone(), '[Gmail]/All Mail'))
        mailbox_uids = self._mb.get_all_uids()
        # TODO: incremental backup: check what's in the targetmbox already
        for uid in mailbox_uids:
            if uids is not None:
                if uid not in uids:
                    continue
            targetmbox = Mailbox.mbox(target)
            targetmbox.lock()
            try:
                print "Backing up UID %s" % uid # DEBUG
                size = self._mb.get_size(uid) # DEBUG
                print "  size = %s" % size # DEBUG
                message = self._mb[uid]
                print "  loaded" # DEBUG
                labels = self.get_labels("[Gmail]/All Mail.%s" % uid)
                for label in labels:
                    try:
                        message.add_header('X-ProcImap-GmailLabel', 
                            str(email.header.make_header([(label, 'ascii')])))
                    except UnicodeDecodeError:
                        message.add_header('X-ProcImap-GmailLabel', 
                            str(email.header.make_header([(label, 'utf-8')])))
                targetmbox.add(message)
                print "  Backed up UID %s" % uid
            except:
                if uids is None:
                    new_uids = [u for u in mailbox_uids if u >= uid ]
                else:
                    new_uids = [u for u in mailbox_uids if u >= uid 
                                and u in uids]
                targetmbox.unlock()
                targetmbox.close()
                return new_uids
            targetmbox.unlock()
            targetmbox.close()
        return []

def is_gmail_box(mailbox):
    """ Return True if the mailbox is on a Gmail server, False otherwise """
    if not isinstance(mailbox, ImapMailbox):
        return False
    return mailbox.server.servername == 'imap.gmail.com'

def get_hash_id(mailbox, uid):
    """ Get the hash_id of the message with the given uid in the mailbox.
        mailbox has to be an instance of ImapMailbox

        The hash_id is computed as the sha224 hash of the message's
        header, appended with a dot and the size of the message in
        bytes.
    """
    sha244hash = hashlib.sha224(mailbox.get_header(uid).as_string()).hexdigest()
    size = mailbox.get_size(uid)
    return "%s.%s" % (sha244hash, size)

def delete(mailbox, uid, backupbox=None):
    """ Delete the message with uid in the mailbox by moving it to the Trash,
    and then deleting it from there.  This removes all copies of the mail from
    other mailboxes on the Gmail server as well.

    If you supply the backupbox option, it must be an opject of type 
    mailbox.Mailbox that is not an ImapMailbox on a gmail server as well. A
    local mbox file is recommended here. If these conditions are not met, a
    TypeError or ValueError will be raised. The email is stored in the backup
    mailbox before being deleted.

    If there is a message in your Trash folder with the same message-id 
    and size as the message to be deleted, a DeleteFromTrashError will be
    thrown.

    Return 0 if mail was removed successfully
    Return 1 if mail was not moved to the Trash folder
    Return 2 if mail was moved to Trash folder, but not removed from there
    """
    if backupbox is not None:
        if not isinstance(backupbox, Mailbox.Mailbox):
            raise TypeError, "backupbox must be of type mailbox.Mailbox."
        if is_gmail_box(backupbox):
            raise ValueError, "backup must not be on a gmail server."
        message = mailbox[uid]
        messageid = message['message-id']
        backupbox.lock()
        backupbox.add(message)
        backupbox.flush()
        backupbox.unlock()
    else:
        header = mailbox.get_header(uid)
        messageid = header['message-id']
    size = mailbox.get_size(uid)
    mailboxname = mailbox.name
    try:
        mailbox.move(uid, '[Gmail]/Trash')
    except:
        return 1
    mailbox.flush()
    try:
        mailbox.switch('[Gmail]/Trash')
        to_delete = mailbox.search("HEADER message-id %s" % messageid)
        to_delete = [m for m in to_delete if mailbox.get_size(m) == size]
        if len(to_delete) > 1:
            raise  DeleteFromTrashError, "Ambigous delete-request on Trash." \
                " Please empty the trash manually."
        for message in to_delete:
            mailbox.set_imapflags(message, '\\Deleted')
    except:
        return 2
    mailbox.flush() 
    mailbox.switch(mailboxname)
    return 0


def restore(source, gmailserver, ids=None):
    """ restore previously backed up emails to the gmailserver.

        source must be an instance of of mailbox.Mailbox that is not an
        ImapMailbox on a gmail server as well. It must point to a mailbox that
        was previously the 'target' in the backup procedure. All mails found in
        the source mailbox will be copied to the '[Gmail]/All Mail' mailbox on
        the gmailserver. If the mails contain a 'X-ProcImap-GmailLabels' header
        field, that field will be removed and the message will be copied to the
        mailboxes specified in the field.

        Return a list of ids from the source that failed to copy to the
        gmailserver.

        If the optional 'ids' argument is given, it has to be a list of id's
        valid in the source mailbox. Only the specified id's will be copied to
        the gmailserver. This is intented for completing a previous restore.
    """
    # TODO: write this
    raise NotImplementedError
    # put message in "All Mails"
    # try to relocate the message in the server
    # if located successfully:
    #   copy to all mailboxes, on server
    # else:
    #   actively upload to all mailboxes


def get_thread(mailbox, uid):
    """ Return a list of uid's from the mailbox that all belong to the same
        conversation as uid. This includes replys, but not necessarilly 
        forwards. The relationship between messages is determined from the 
        message ID and the appropriate reference headers.
        The result list will be sorted.
        You can use this on a non-Gmail server, too, provided that the
        messages on your server have proper message ids. Note that in some
        instances, Gmail (the web interface) will count an email as belonging
        to a certain thread without relying on the message-id references in 
        the headers. This happens with forwards, for example.
        Also, remember that you will only find messages that are in the
        mailbox. So, for complete threads, you should be in 
        '[Gmail]/All Mail'. On non-Gmail servers, you won't be able to
        get complete threads unless you keep all your messages in one
        imap folder.
    """
    thread_uids = set()
    open_ids = set() # id's with unprocessed references
    closed_ids = set() # id's with all references processed
    # look at the "past" of uid (messages referenced by uid)
    header = mailbox.get_header(uid)
    for id in references_from_header(header):
        open_ids.add(id)
    thread_uids.add(uid)
    id = mailbox.get_fields(uid, 'message-id')['message-id'] 
    closed_ids.add(id)
    # look at the "future" of uid (messages referencing uid)
    referencing_uids = mailbox.search(
        "OR (HEADER references %s) (HEADER in-reply-to %s)" % (id, id))
    for referencing_uid in referencing_uids:
        id = mailbox.get_fields(referencing_uid, 'message-id')['message-id'] 
        if id not in closed_ids:
            open_ids.add(id)
        thread_uids.add(referencing_uid)
    # go through all the ids that were found so far, and get their references
    # in turn
    while len(open_ids) > 0:
        open_id = open_ids.pop()
        uids = mailbox.search("HEADER message-id %s" % open_id)
        for uid in uids: 
            # there should only be one uid, unless there are duplicate ids
            thread_uids.add(uid)
        # for open ids, we only need to look into the "future"; the "past"
        # is guaranteed to be known already FIXME: THIS IS INCORRECT
        referencing_uids = mailbox.search(
            "OR (HEADER references %s) (HEADER in-reply-to %s)" 
            % (open_id, open_id))
        for referencing_uid in referencing_uids:
            id = mailbox.get_fields(referencing_uid, 
                                    'message-id')['message-id'] 
            if id not in closed_ids:
                open_ids.add(id)
        closed_ids.add(open_id)
    result = list(thread_uids)
    result.sort()
    return result

def get_labels(mailbox, uid):
    """ Return the list of mailboxes on the server that contain the message
        with uid. As in general, message identity is established by the
        message id and the message size
    """
    raise NotImplementedError
