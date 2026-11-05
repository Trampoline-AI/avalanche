"""Prepare real Deno/Pyodide assets before concurrent agent scenarios."""

import asyncio

from predict_rlm.backends.jspi import JspiBackend


async def main() -> None:
    backend = JspiBackend()
    try:
        result = await backend.aexecute("print('JSPI assets ready')")
        assert "JSPI assets ready" in str(result), result
        print(result)
    finally:
        await backend.ashutdown()


if __name__ == "__main__":
    asyncio.run(main())
