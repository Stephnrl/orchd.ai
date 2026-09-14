# Local operator sessions

The loopback server has one process-local bearer session. The startup token is
printed once for the operator and held in memory; it is not a provider credential,
user account, or durable approval. Keep the terminal and token private.

The default session expires after one hour without authenticated application
activity, or eight hours after server startup, whichever comes first. Normal UI
polling counts as activity. Checking session status alone does not refresh the
idle deadline. Invalid tokens do not extend it. Expiry is irreversible within
that server process, and rotation never extends the absolute deadline.

The workspace's **Operator session** controls provide:

- **Check session**: show the generation and remaining time without returning the token.
- **Rotate session**: replace the token immediately, invalidating every old client.
  The current tab retains the replacement only in memory and clears task selection
  and review state. It must reload tasks and review evidence again.
- **End server session**: revoke access for every client. Restart the server to
  obtain a new session. Revocation does not cancel work already admitted by the broker.

**Disconnect** only clears this browser tab; it does not revoke other clients.
Reloading after rotation loses the replacement token. If a rotation response is
lost, or the session expires or is revoked, restart the server for a new startup
token. Reconnect and inspect durable task state before taking further action.
Session controls do not renew or replace workflow approvals.

All endpoints below require `Authorization: Bearer TOKEN`; POST requests use
`Content-Type: application/json` and an empty JSON object.

| Endpoint | Result |
| --- | --- |
| `GET /session` | Policy, generation and remaining seconds; no secret |
| `POST /session/rotate` | Replacement `session` token and status, delivered once to the caller |
| `POST /session/revoke` | Revocation confirmation and restart requirement |

Responses use `Cache-Control: no-store`. The UI uses neither local nor session
storage for tokens. An authentication failure clears displayed evidence and
review state and returns to login. Responses belonging to an older token cannot
clear a newer connection or populate its view.

The HTTP boundary rejects duplicate authentication/framing headers, transfer or
content encoding, malformed lengths, duplicate JSON keys, non-finite JSON numbers,
invalid UTF-8 and non-object JSON bodies. Protected requests authenticate before
reading their body; JSON remains bounded to 4 KiB and bundle uploads retain their
separate limits and expected-digest checks. Existing Host/Origin restrictions,
loopback binding and browser security headers remain enforced.

This milestone does not provide multiple operator identities, SSO, a credential
vault, live integration credentials or production deployment approval. Host
administrators remain trusted. The server remains a bounded local application;
independent deployment and credential-boundary review are still required.
