"""Send PDF attachments to Kindle via Gmail API."""

import base64
import logging
import os
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

SCOPES = ["https://mail.google.com/"]


def create_gmail_service(credentials_file="credentials.json", token_file="token.json"):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def send_to_kindle(sender: str, to: str, pdf_file: str, file_name: str):
    service = create_gmail_service()
    msg = MIMEMultipart()
    msg["from"] = sender
    msg["to"] = to
    msg["subject"] = f"Sending to Kindle Scribe: {file_name}"
    msg.attach(MIMEText("See attachments", "plain"))

    attachment = MIMEBase("application", "pdf")
    with open(pdf_file, "rb") as f:
        attachment.set_payload(f.read())
    attachment.add_header("Content-Disposition", "attachment", filename=file_name)
    encoders.encode_base64(attachment)
    msg.attach(attachment)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    result = service.users().messages().send(userId=sender, body={"raw": raw}).execute()
    log.info("Message sent, id: %s", result["id"])
    return result
