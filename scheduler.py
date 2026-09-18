import time

from redis_storage import (
    get_due_tasks,
    save_task
)

from telegram_sender import (
    create_client,
    send_to_groups
)


async def process_tasks():

    tasks = get_due_tasks()

    if not tasks:

        print("Задач для выполнения нет.")

        return {
            "processed": 0,
            "message": "No due tasks"
        }

    print(
        f"Найдено задач: {len(tasks)}"
    )

    client = None

    processed = 0

    try:

        for task in tasks:

            print(
                f"Обрабатываем задачу: "
                f"{task['id']}"
            )

            try:

                if client is None:

                    client = (
                        await create_client()
                    )

                result = await send_to_groups(
                    client,
                    task["message"],
                    task["groups"]
                )

                print(
                    f"Успешно: "
                    f"{result['success']}"
                )

                print(
                    f"Ошибок: "
                    f"{result['failed']}"
                )

                # Увеличиваем счётчик
                task["completed_repeats"] += 1

                # Все повторы выполнены
                if (
                    task["completed_repeats"]
                    >= task["total_repeats"]
                ):

                    task["status"] = "completed"

                    print(
                        f"Задача {task['id']} "
                        f"завершена."
                    )

                else:

                    # Следующий запуск
                    task["next_run"] = (
                        time.time()
                        + task["interval_minutes"]
                        * 60
                    )

                    print(
                        "Следующий запуск через "
                        f"{task['interval_minutes']} мин."
                    )

                save_task(task)

                processed += 1

            except Exception as e:

                print(
                    f"Ошибка задачи "
                    f"{task['id']}: {e}"
                )

    finally:

        if client:

            await client.disconnect()

    return {
        "processed": processed
    }