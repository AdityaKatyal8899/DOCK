"""A tiny HTTP service for testing Dock's build and container workflow."""

from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"Hello from Dock test server!\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print(f"{self.address_string()} - {format % args}")


if __name__ == "__main__":
    print("Dock test server listening on 0.0.0.0:8000", flush=True)
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
