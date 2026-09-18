import asyncio

from scheduler import process_tasks


async def main():

    result = await process_tasks()

    print()
    print("Результат:")
    print(result)


if __name__ == "__main__":

    asyncio.run(main())