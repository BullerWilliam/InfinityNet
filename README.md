# InfinityNet
InfinityNet is a tiny Node.js HTTP server that lets you send requests to a
custom "ip-like" address and later reply to those requests. It is designed to
run locally or on Render with zero external dependencies.

## What it does
- `POST /send/{ip-like}` stores a request under that ip-like value.
- `GET /send/{ip-like}` stores a request under that ip-like value.
- `GET /server/{ip-like}` returns only pending (unresponded) requests.
- `POST /server/{ip-like}` marks a request as responded and stores a response.

An "ip-like" value is any string of digits separated by dots, such as
`1.2.3.4` or `10.99.3.7`.

You can also use custom subpaths after the ip-like value, for example:
`/send/1.2.3.4/chat/main` and `/server/1.2.3.4/chat/main`.

## Quick start (local)
1. Ensure Node.js 18+ is installed.
2. Run the server:
```bash
node server.js
```
3. The server listens on `http://localhost:3000` by default.
4. On Windows, you can run `run.bat` to force port 3000 (it will stop any process already listening on 3000).

## Deploy on Render
1. Create a new Web Service.
2. Connect this repo.
3. Use these settings:
   - Build Command: leave empty
   - Start Command: `node server.js`
4. Render will set `PORT` automatically. The server reads `process.env.PORT`.

## Endpoint details
### POST /send/{ip-like}
Stores a request and **waits until the server replies** via `POST /server/{ip-like}`.
- Body: JSON or plain text (anything). If JSON, it is parsed and stored.
- Query string: supported. Query values are stored alongside the request.
- Response (after server responds):
```json
{ "ok": true, "id": "request-id", "response": "got it", "respondedAt": "2026-03-13T18:02:00.000Z" }
```
If no response arrives within the wait timeout, the request returns:
```json
{ "ok": false, "id": "request-id", "error": "Response timeout" }
```

### GET /send/{ip-like}
Acts like a send request and **waits until the server replies**, just like `POST /send/{ip-like}`.
The query string (if provided) is stored on the request entry.

### GET /server/{ip-like}
Returns only pending (not yet responded) requests.
- Response:
```json
{
  "ok": true,
  "ip": "1.2.3.4",
  "pending": [
    {
      "id": "request-id",
      "method": "POST",
      "data": { "message": "hello" },
      "query": { "source": "web" },
      "receivedAt": "2026-03-13T18:00:00.000Z",
      "responded": false,
      "response": null,
      "respondedAt": null
    }
  ]
}
```

### POST /server/{ip-like}
Responds to a specific request by id.
- Body: JSON with `id` and `response`
```json
{ "id": "request-id", "response": "got it" }
```
- Response:
```json
{ "ok": true }
```

## Example requests
```bash
curl -X POST http://localhost:3000/send/1.2.3.4 \
  -H "Content-Type: application/json" \
  -d "{\"message\":\"hello\"}"

curl "http://localhost:3000/send/1.2.3.4?source=web&user=tim"

curl http://localhost:3000/server/1.2.3.4

curl -X POST http://localhost:3000/server/1.2.3.4 \
  -H "Content-Type: application/json" \
  -d "{\"id\":\"YOUR_ID\",\"response\":\"got it\"}"
```

## Notes and limits
- All data is stored in memory. It resets whenever the server restarts.
- Maximum request body size is 1 MB.
- CORS is enabled for all origins.
- `POST /send/{ip-like}` will wait up to `WAIT_TIMEOUT_MS` (default: 60000).
- Set `FORCE_PORT_3000=1` to force port 3000 even if `PORT` is set.
