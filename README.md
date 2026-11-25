# predloshka

Telegram-бот предложки для канала. Пользователи присылают посты в личку боту, администраторы группы одобряют или отклоняют их, одобренные публикуются в канал. Поддерживает массовую рассылку.

## Стек

- Python 3, aiogram 3
- Docker / docker-compose

## Запуск

```bash
cp .env.example .env
# вставь BOT_TOKEN в .env
docker compose up -d
```
