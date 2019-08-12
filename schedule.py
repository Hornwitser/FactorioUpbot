from asyncio import CancelledError, sleep
from sys import stderr
from time import time
from traceback import print_exc

async def repeat(coro, delta):
    """runs coro exactly every delta seconds"""
    target = time()
    while True:
        try:
            await coro()
        except CancelledError:
            raise
        except Exception:
            print("Ignoring exepction in repated coroutine", file=stderr)
            print_exc()

        current = time()
        skip = int((current - target) // delta) + 1
        target = target + delta * skip
        wait = target - current

        if skip > 1:
            print(f"Skipping {skip-1} target times in the past", file=stderr)

        await sleep(wait)

if __name__ == '__main__':
    from asyncio import run, create_task
    from random import uniform

    async def test_time():
        print(f"{time() % 1}")
        await sleep(uniform(0.5, 2.0))
        if uniform(0, 1) > 0.7:
            raise ValueError("Did a badie")
        print("Task done")

    async def test_main():
        task = create_task(repeat(test_time, 1))
        await sleep(20)

    run(test_main())

