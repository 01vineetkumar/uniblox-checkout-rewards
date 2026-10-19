"""Private smoke-test runner with a portable graceful shutdown channel."""

import sys
from threading import Thread

import uvicorn


def main():
    server = uvicorn.Server(
        uvicorn.Config(
            "checkout.main:app",
            host="127.0.0.1",
            port=int(sys.argv[1]),
            access_log=False,
        )
    )

    def stop_on_input():
        sys.stdin.readline()
        server.should_exit = True

    Thread(target=stop_on_input, daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()
