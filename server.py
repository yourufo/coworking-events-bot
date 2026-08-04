from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse
from html import escape


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        params = {key: values[0] if values else "" for key, values in query.items()}

        if parsed.path == "/callback":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(build_callback_page(params).encode("utf-8"))
            return

        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Not Found")

    def log_message(self, format, *args):
        return


def build_callback_page(params):
    code = escape(params.get("code", ""))
    state = escape(params.get("state", ""))
    return f"""<!DOCTYPE html>
<html lang=\"ru\">
  <head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Callback</title>
    <style>
      body {{ font-family: Arial, sans-serif; margin: 2rem; }}
      .card {{ max-width: 640px; padding: 1.5rem; border: 1px solid #ddd; border-radius: 12px; }}
    </style>
  </head>
  <body>
    <div class=\"card\">
      <h1>Callback received</h1>
      <p>Ссылка успешно обработана.</p>
      <p><strong>code:</strong> {code}</p>
      <p><strong>state:</strong> {state}</p>
    </div>
  </body>
</html>"""


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 42241), CallbackHandler)
    print("Server running on http://127.0.0.1:42241")
    server.serve_forever()
