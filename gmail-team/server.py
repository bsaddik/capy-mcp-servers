#!/usr/bin/env python3
"""
Gmail IMAP/SMTP MCP server for one Google Workspace mailbox (Amtec team edition).
Credentials come from environment variables, set on the MCP server in Capy:
  GOOGLE_MAIL_ADDRESS       the mailbox, e.g. name@amtecai.com
  GOOGLE_MAIL_APP_PASSWORD  a Gmail app password (needs 2-Step Verification)
Fallback: ~/.config/amtec-mail/credentials.json with keys email, app_password.
Runs as an MCP stdio server by default; "cli search|read|folders|draft|send" for manual use.
Setup guide: the google-setup skill in the Amtec organisation drive.
"""

import argparse
import email
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import html
import imaplib
import json
import os
import pathlib
import re
import smtplib
import sys
import time
from typing import Any, Dict, List, Optional


CRED_PATH = pathlib.Path.home() / ".config" / "amtec-mail" / "credentials.json"



def load_credentials() -> Dict[str, Any]:
    defaults = {"imap_host": "imap.gmail.com", "imap_port": 993, "smtp_host": "smtp.gmail.com", "smtp_port": 465}
    address = os.environ.get("GOOGLE_MAIL_ADDRESS", "").strip()
    password = os.environ.get("GOOGLE_MAIL_APP_PASSWORD", "").replace(" ", "").strip()
    if address and password:
        return {**defaults, "email": address, "app_password": password}
    if CRED_PATH.exists():
        return {**defaults, **json.loads(CRED_PATH.read_text())}
    raise RuntimeError(
        "No Gmail credentials. Set GOOGLE_MAIL_ADDRESS and GOOGLE_MAIL_APP_PASSWORD on this MCP server "
        "(Capy Settings > MCP & plugins). See the google-setup skill."
    )


def decode_mime_header(val: Optional[str]) -> str:
    if not val:
        return ""
    try:
        return str(make_header(decode_header(val)))
    except Exception:
        return val


def strip_html(raw_html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?(</\1>)", "", raw_html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def extract_body_and_attachments(msg: email.message.Message) -> Dict[str, Any]:
    plain_parts: List[str] = []
    html_parts: List[str] = []
    attachments: List[Dict[str, Any]] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get("Content-Disposition") or "")
            filename = part.get_filename()
            if filename or "attachment" in cdisp.lower():
                payload = part.get_payload(decode=True) or b""
                attachments.append(
                    {
                        "filename": decode_mime_header(filename) or "unnamed",
                        "content_type": ctype,
                        "size_bytes": len(payload),
                    }
                )
                continue
            if ctype == "text/plain":
                payload = part.get_payload(decode=True)
                if payload is not None:
                    charset = part.get_content_charset() or "utf-8"
                    plain_parts.append(payload.decode(charset, errors="replace"))
            elif ctype == "text/html":
                payload = part.get_payload(decode=True)
                if payload is not None:
                    charset = part.get_content_charset() or "utf-8"
                    html_parts.append(payload.decode(charset, errors="replace"))
    else:
        ctype = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload is not None:
            charset = msg.get_content_charset() or "utf-8"
            decoded = payload.decode(charset, errors="replace")
            if ctype == "text/html":
                html_parts.append(decoded)
            else:
                plain_parts.append(decoded)

    body = "\n".join(plain_parts).strip()
    if not body and html_parts:
        body = strip_html("\n".join(html_parts))

    return {"body": body, "attachments": attachments}


def connect_imap() -> imaplib.IMAP4_SSL:
    creds = load_credentials()
    M = imaplib.IMAP4_SSL(creds.get("imap_host", "imap.gmail.com"), int(creds.get("imap_port", 993)))
    M.login(creds["email"], creds["app_password"])
    return M


def quote_imap_mailbox(mailbox: str) -> str:
    if mailbox.startswith('"') and mailbox.endswith('"'):
        return mailbox
    return f'"{mailbox}"'


def list_folders() -> List[str]:
    M = connect_imap()
    try:
        typ, data = M.list()
        folders = []
        for row in data or []:
            if not row:
                continue
            line = row.decode("utf-8", errors="replace")
            m = re.search(r'"/"\s+"?([^"]+)"?$', line)
            folders.append(m.group(1) if m else line)
        return folders
    finally:
        M.logout()


def search_emails(
    query: str = "",
    folder: str = "INBOX",
    limit: int = 20,
) -> List[Dict[str, Any]]:
    M = connect_imap()
    try:
        typ, _ = M.select(quote_imap_mailbox(folder), readonly=True)
        if typ != "OK":
            raise RuntimeError(f"Could not select folder {folder}")

        if query.strip():
            escaped = query.replace("\\", "\\\\").replace('"', '\\"')
            typ, data = M.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW", f'"{escaped}"')
        else:
            typ, data = M.uid("SEARCH", None, "ALL")

        if typ != "OK" or not data or not data[0]:
            return []

        uids = data[0].split()
        selected_uids = list(reversed(uids[-limit:]))
        if not selected_uids:
            return []

        uid_set = b",".join(selected_uids)
        typ, fetch_data = M.uid(
            "FETCH",
            uid_set,
            "(UID FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID)])",
        )
        results_by_uid: Dict[str, Dict[str, Any]] = {}
        for item in fetch_data or []:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            meta = item[0].decode("utf-8", errors="replace")
            uid_match = re.search(r"UID\s+(\d+)", meta)
            if not uid_match:
                continue
            uid_str = uid_match.group(1)
            hdr_msg = email.message_from_bytes(item[1])
            results_by_uid[uid_str] = {
                "uid": uid_str,
                "folder": folder,
                "subject": decode_mime_header(hdr_msg.get("Subject")),
                "from": decode_mime_header(hdr_msg.get("From")),
                "to": decode_mime_header(hdr_msg.get("To")),
                "cc": decode_mime_header(hdr_msg.get("Cc")),
                "date": decode_mime_header(hdr_msg.get("Date")),
                "message_id": (hdr_msg.get("Message-ID") or "").strip(),
            }

        ordered = []
        for u in selected_uids:
            u_str = u.decode()
            if u_str in results_by_uid:
                ordered.append(results_by_uid[u_str])
        return ordered
    finally:
        M.logout()


def read_email(uid: str, folder: str = "INBOX") -> Dict[str, Any]:
    M = connect_imap()
    try:
        typ, _ = M.select(quote_imap_mailbox(folder), readonly=True)
        if typ != "OK":
            raise RuntimeError(f"Could not select folder {folder}")
        typ, fetch_data = M.uid("FETCH", uid.encode(), "(UID FLAGS BODY.PEEK[])")
        if typ != "OK" or not fetch_data:
            raise RuntimeError(f"Could not fetch UID {uid} in {folder}")

        raw_bytes = None
        for item in fetch_data:
            if isinstance(item, tuple) and len(item) >= 2:
                raw_bytes = item[1]
                break
        if not raw_bytes:
            raise RuntimeError(f"No message found for UID {uid} in {folder}")

        msg = email.message_from_bytes(raw_bytes)
        extracted = extract_body_and_attachments(msg)
        return {
            "uid": str(uid),
            "folder": folder,
            "subject": decode_mime_header(msg.get("Subject")),
            "from": decode_mime_header(msg.get("From")),
            "to": decode_mime_header(msg.get("To")),
            "cc": decode_mime_header(msg.get("Cc")),
            "date": decode_mime_header(msg.get("Date")),
            "message_id": (msg.get("Message-ID") or "").strip(),
            "in_reply_to": (msg.get("In-Reply-To") or "").strip(),
            "references": (msg.get("References") or "").strip(),
            "body": extracted["body"],
            "attachments": extracted["attachments"],
        }
    finally:
        M.logout()


def build_message(
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> EmailMessage:
    creds = load_credentials()
    msg = EmailMessage()
    msg["From"] = creds["email"]
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="amtecai.com")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    return msg


def create_draft(
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> Dict[str, Any]:
    msg = build_message(to=to, subject=subject, body=body, cc=cc, in_reply_to=in_reply_to, references=references)
    M = connect_imap()
    try:
        typ, data = M.append('"[Gmail]/Drafts"', "\\Draft", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        return {"status": typ, "folder": "[Gmail]/Drafts", "to": to, "subject": subject, "response": str(data)}
    finally:
        M.logout()


def send_email(
    to: str,
    subject: str,
    body: str,
    cc: Optional[str] = None,
    bcc: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
) -> Dict[str, Any]:
    creds = load_credentials()
    msg = build_message(to=to, subject=subject, body=body, cc=cc, bcc=bcc, in_reply_to=in_reply_to, references=references)
    with smtplib.SMTP_SSL(creds.get("smtp_host", "smtp.gmail.com"), int(creds.get("smtp_port", 465))) as smtp:
        smtp.login(creds["email"], creds["app_password"])
        smtp.send_message(msg)
    return {"status": "sent", "from": creds["email"], "to": to, "cc": cc, "subject": subject, "message_id": msg["Message-ID"]}


TOOLS = [
    {
        "name": "search_emails",
        "description": "Search or list emails in the connected Gmail account using Gmail search syntax (e.g. 'from:someone', 'subject:proposal', 'is:unread', 'newer_than:7d', or empty string for latest emails).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Gmail search query (X-GM-RAW). Leave empty to return the most recent emails in the folder.",
                },
                "folder": {
                    "type": "string",
                    "description": "IMAP folder name, e.g. 'INBOX', '[Gmail]/Sent Mail', '[Gmail]/All Mail', '[Gmail]/Drafts'. Defaults to 'INBOX'.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of emails to return (newest first). Defaults to 20.",
                },
            },
        },
    },
    {
        "name": "read_email",
        "description": "Read the full body, headers, and attachment list of an email in the connected Gmail account by its IMAP UID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "uid": {"type": "string", "description": "IMAP UID of the email (returned by search_emails)."},
                "folder": {
                    "type": "string",
                    "description": "IMAP folder where the UID resides. Defaults to 'INBOX'.",
                },
            },
            "required": ["uid"],
        },
    },
    {
        "name": "list_folders",
        "description": "List all Gmail IMAP folders/labels for the connected Gmail account.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_draft",
        "description": "Save a draft email in the connected account's [Gmail]/Drafts folder without sending it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address(es), comma-separated."},
                "subject": {"type": "string", "description": "Email subject line."},
                "body": {"type": "string", "description": "Plain text email body."},
                "cc": {"type": "string", "description": "Optional CC email address(es)."},
                "in_reply_to": {"type": "string", "description": "Optional Message-ID header of the email being replied to."},
                "references": {"type": "string", "description": "Optional References header for threading."},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "send_email",
        "description": "Send an email from the connected Gmail account via Gmail SMTP.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address(es), comma-separated."},
                "subject": {"type": "string", "description": "Email subject line."},
                "body": {"type": "string", "description": "Plain text email body."},
                "cc": {"type": "string", "description": "Optional CC email address(es)."},
                "bcc": {"type": "string", "description": "Optional BCC email address(es)."},
                "in_reply_to": {"type": "string", "description": "Optional Message-ID header of the email being replied to."},
                "references": {"type": "string", "description": "Optional References header for threading."},
            },
            "required": ["to", "subject", "body"],
        },
    },
]


def handle_tool_call(name: str, args: Dict[str, Any]) -> Any:
    if name == "search_emails":
        return search_emails(
            query=args.get("query", ""),
            folder=args.get("folder", "INBOX"),
            limit=int(args.get("limit", 20)),
        )
    if name == "read_email":
        return read_email(uid=str(args["uid"]), folder=args.get("folder", "INBOX"))
    if name == "list_folders":
        return list_folders()
    if name == "create_draft":
        return create_draft(
            to=args["to"],
            subject=args["subject"],
            body=args["body"],
            cc=args.get("cc"),
            in_reply_to=args.get("in_reply_to"),
            references=args.get("references"),
        )
    if name == "send_email":
        return send_email(
            to=args["to"],
            subject=args["subject"],
            body=args["body"],
            cc=args.get("cc"),
            bcc=args.get("bcc"),
            in_reply_to=args.get("in_reply_to"),
            references=args.get("references"),
        )
    raise ValueError(f"Unknown tool: {name}")


def run_mcp_stdio() -> None:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue

        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params") or {}

        if req_id is None:
            continue

        try:
            if method == "initialize":
                client_proto = params.get("protocolVersion") or "2024-11-05"
                res = {
                    "protocolVersion": client_proto,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "amtec-mail", "version": "1.0.0"},
                }
            elif method == "ping":
                res = {}
            elif method == "tools/list":
                res = {"tools": TOOLS}
            elif method == "resources/list":
                res = {"resources": []}
            elif method == "prompts/list":
                res = {"prompts": []}
            elif method == "tools/call":
                t_name = params.get("name", "")
                t_args = params.get("arguments") or {}
                out = handle_tool_call(t_name, t_args)
                res = {
                    "content": [{"type": "text", "text": json.dumps(out, indent=2, ensure_ascii=False)}],
                    "isError": False,
                }
            else:
                sys.stdout.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {"code": -32601, "message": f"Method not found: {method}"},
                        }
                    )
                    + "\n"
                )
                sys.stdout.flush()
                continue

            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": res}) + "\n")
            sys.stdout.flush()
        except Exception as e:
            if method == "tools/call":
                sys.stdout.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "content": [{"type": "text", "text": f"Error: {e}"}],
                                "isError": True,
                            },
                        }
                    )
                    + "\n"
                )
            else:
                sys.stdout.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {"code": -32000, "message": str(e)},
                        }
                    )
                    + "\n"
                )
            sys.stdout.flush()


def run_cli(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(prog="amtec-mail")
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="Search or list emails")
    p_search.add_argument("positional_query", nargs="?", default="", help="Optional Gmail search query")
    p_search.add_argument("--query", "-q", default=None, help="Gmail search query (X-GM-RAW)")
    p_search.add_argument("--folder", "-f", default="INBOX", help="IMAP folder (default: INBOX)")
    p_search.add_argument("--limit", "-l", type=int, default=20, help="Max emails to return (default: 20)")

    p_read = sub.add_parser("read", help="Read an email by UID")
    p_read.add_argument("uid", help="IMAP UID")
    p_read.add_argument("--folder", "-f", default="INBOX", help="IMAP folder (default: INBOX)")

    sub.add_parser("folders", help="List IMAP folders")

    p_draft = sub.add_parser("draft", help="Create a draft in [Gmail]/Drafts")
    p_draft.add_argument("--to", required=True, help="Recipient email(s)")
    p_draft.add_argument("--subject", required=True, help="Subject line")
    p_draft.add_argument("--body", default=None, help="Email body string")
    p_draft.add_argument("--body-file", default=None, help="Path to file containing email body")
    p_draft.add_argument("--cc", default=None, help="Optional CC email(s)")
    p_draft.add_argument("--in-reply-to", default=None, help="Optional In-Reply-To Message-ID")
    p_draft.add_argument("--references", default=None, help="Optional References header")

    p_send = sub.add_parser("send", help="Send an email from the connected Gmail account")
    p_send.add_argument("--to", required=True, help="Recipient email(s)")
    p_send.add_argument("--subject", required=True, help="Subject line")
    p_send.add_argument("--body", default=None, help="Email body string")
    p_send.add_argument("--body-file", default=None, help="Path to file containing email body")
    p_send.add_argument("--cc", default=None, help="Optional CC email(s)")
    p_send.add_argument("--bcc", default=None, help="Optional BCC email(s)")
    p_send.add_argument("--in-reply-to", default=None, help="Optional In-Reply-To Message-ID")
    p_send.add_argument("--references", default=None, help="Optional References header")

    args = parser.parse_args(argv)
    if args.command == "search":
        q = args.query if args.query is not None else args.positional_query
        print(json.dumps(search_emails(query=q, folder=args.folder, limit=args.limit), indent=2, ensure_ascii=False))
    elif args.command == "read":
        print(json.dumps(read_email(uid=args.uid, folder=args.folder), indent=2, ensure_ascii=False))
    elif args.command == "folders":
        print(json.dumps(list_folders(), indent=2, ensure_ascii=False))
    elif args.command in ("draft", "send"):
        body = args.body
        if args.body_file:
            body = pathlib.Path(args.body_file).read_text()
        if body is None:
            parser.error("Provide --body or --body-file")
        if args.command == "draft":
            res = create_draft(
                to=args.to,
                subject=args.subject,
                body=body,
                cc=args.cc,
                in_reply_to=args.in_reply_to,
                references=args.references,
            )
        else:
            res = send_email(
                to=args.to,
                subject=args.subject,
                body=body,
                cc=args.cc,
                bcc=args.bcc,
                in_reply_to=args.in_reply_to,
                references=args.references,
            )
        print(json.dumps(res, indent=2, ensure_ascii=False))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "cli":
        run_cli(sys.argv[2:])
    else:
        run_mcp_stdio()


if __name__ == "__main__":
    main()
