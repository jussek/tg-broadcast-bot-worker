from redis_storage import (
    save_last_message,
    get_last_message,
    create_task,
    get_user_tasks
)


USER_ID = 123456789


save_last_message(
    USER_ID,
    "Тестовое последнее сообщение"
)


print(
    "Последнее сообщение:"
)

print(
    get_last_message(USER_ID)
)


task = create_task(

    user_id=USER_ID,

    message="Тестовая рассылка",

    groups=[
        -100111111111,
        -100222222222
    ],

    interval_minutes=20,

    repeats=5
)


print()
print("Созданная задача:")
print(task)


print()
print("Задачи пользователя:")

print(
    get_user_tasks(USER_ID)
)
