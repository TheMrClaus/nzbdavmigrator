#!/usr/bin/env python3
"""
Simplified web server for debugging Docker issues
"""

from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import os

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()

            html = """
            <html>
            <head><title>NZBDAVMigrator Debug</title></head>
            <body>
                <h1>NZBDAVMigrator Debug Page</h1>
                <p>Container is running successfully!</p>
                <h2>Environment:</h2>
                <ul>
                    <li>Working Directory: {cwd}</li>
                    <li>User: {user}</li>
                    <li>Data Directory Exists: {data_exists}</li>
                    <li>Database Path: {db_path}</li>
                </ul>
                <h2>Files in /app:</h2>
                <ul>
                    {files}
                </ul>
            </body>
            </html>
            """.format(
                cwd=os.getcwd(),
                user=os.getenv('USER', 'unknown'),
                data_exists=os.path.exists('/app/data'),
                db_path=os.getenv('STATUS_DB', 'data/nzbdav_status.db'),
                files=''.join(f'<li>{f}</li>' for f in os.listdir('/app'))
            )

            self.wfile.write(html.encode())
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        print(f"Request: {format % args}")

def main():
    print("Starting simple debug web server on port 9999...")
    print(f"Working directory: {os.getcwd()}")
    print(f"Files in current directory: {os.listdir('.')}")
    print(f"Data directory exists: {os.path.exists('data')}")

    try:
        server = HTTPServer(('0.0.0.0', 9999), SimpleHandler)
        print("Debug server started successfully")
        server.serve_forever()
    except Exception as e:
        print(f"Failed to start server: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()