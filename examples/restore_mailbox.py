#!/usr/bin/env python
"""
    This example shows how to restore a backup created by backup_mailbox.py"
"""
from ProcImap.ImapMailbox import ImapMailbox
from ProcImap.ImapMessage import ImapMessage
from ProcImap.Utils.MailboxFactory import MailboxFactory
from mailbox import mbox
import sys

# usage: restore_mailbox.py backupmbox imapmailbox

mailboxes = MailboxFactory('/home/goerz/.procimap/mailboxes.cfg')
server = mailboxes.get_server('Gmail')
mailbox = ImapMailbox((server, sys.argv[2]))
backupsource = mbox(sys.argv[1], factory=ImapMessage)

for message in backupsource:
    if "X-ProcImap-Imapflags" in message:
        message.flags_from_string(message["X-ProcImap-Imapflags"])
        del message["X-ProcImap-Imapflags"]
    if "X-ProcImap-ImapInternalDate" in message:
        message.internaldate_from_string(message["X-ProcImap-ImapInternalDate"])
        del message["X-ProcImap-ImapInternalDate"]
    mailbox.add(message)

mailbox.close()
backupsource.close()
sys.exit(0)
