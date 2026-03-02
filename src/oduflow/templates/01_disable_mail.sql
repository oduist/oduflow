-- Delete incoming mail servers (fetchmail)
DELETE FROM fetchmail_server;

-- Delete outgoing mail servers
DELETE FROM ir_mail_server;
