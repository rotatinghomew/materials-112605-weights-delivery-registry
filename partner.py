"""在本机提供对接方已经留存的回执。"""
import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


DATA_PATH = Path(__file__).with_name("fixtures") / "exchange.json"


def parse_time(text):
    instant = datetime.fromisoformat(text)
    if instant.utcoffset() is None:
        raise ValueError("时间须包含时区偏移")
    return instant


def handler_for(receipts, unavailable):
    class ReceiptHandler(BaseHTTPRequestHandler):
        def reply(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            target = urlsplit(self.path)
            if target.path == "/health":
                self.reply(200, {"status": "ok"})
                return
            if target.path != "/v1/receipts":
                self.reply(404, {"message": "路径不存在"})
                return
            times = parse_qs(target.query).get("at", [])
            try:
                until = parse_time(times[0]) if len(times) == 1 else None
                if until is None:
                    raise ValueError
            except ValueError:
                self.reply(400, {"message": "请提供一个带时区的 at 参数"})
                return
            if unavailable:
                self.reply(503, {"message": "对接方暂时无法查阅回执"})
                return
            visible = [row for row in receipts if parse_time(row["arrived"]) <= until]
            self.reply(200, {"receipts": visible})

        def log_message(self, format, *args):
            pass

    return ReceiptHandler


def main():
    parser = argparse.ArgumentParser(description="本机对接方回执回放")
    parser.add_argument("--port", type=int, default=8087, help="回环地址端口，0 表示自动分配")
    parser.add_argument("--unavailable", action="store_true", help="模拟回执查询暂时不能使用")
    parser.add_argument("--data", type=Path, default=DATA_PATH, help="只读取的交换资料")
    args = parser.parse_args()
    receipts = json.loads(args.data.read_text(encoding="utf-8"))["receipts"]
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(receipts, args.unavailable)) as server:
        print(f"http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
