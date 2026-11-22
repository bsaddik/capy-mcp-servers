# capy-mcp-servers

MCP servers that Capy starts with `uv run --quiet <raw URL pinned to a commit>`. No credentials live here; each server reads them from environment variables set on the MCP server in Capy.

| Path | Capy server | Credentials (env) |
| --- | --- | --- |
| `wise/wise_mcp.py` | Wise (read-only Wise API) | `WISE_API_TOKEN` |
| `gmail/mcp_server.py` | Gmail (benji@amtecai.com) | `AMTECAI_MAIL_CREDENTIALS` |
| `gmail-team/server.py` | Gmail for teammates (org skill `google-setup`) | `GOOGLE_MAIL_ADDRESS`, `GOOGLE_MAIL_APP_PASSWORD` |
| `google-health/server.py` | Coach Google Health (read-only sleep, HRV, resting heart rate); `google-health/authorize.py` makes the credentials | `COACH_HEALTH_CREDENTIALS` |

Capy pins each URL to a commit, so pushing here changes nothing until the server's URL is updated in Capy.
