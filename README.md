# ImagerBOT

ImagerBOT - телеграм-приложение для генерации картинок из картинок, реализованный с помощью микросервисной архитектуры.

## Микросервис Imager (Progress 85%)
Imager (imager/) — это основная логика сборки приложений, основанная на архитектуре DDD (Domain-Driven Design) и реализующая чистую архитектуру.

### Пример работы приложения

[Оригинал картинки](https://disk.yandex.ru/d/riH4YCdRikEelQ)

[Собранная IMager картинка](https://disk.yandex.ru/i/TVr7V8AwB1QTDA)

### Технологии
- **Python 3.12**
- **MongoDB**
- **FastAPI**
- **Docker**
- **Postman**
- **Pytest**
- **Flake8**
- **Loguru**
- **Asyncio**

### Паттерны
- **Result**
- **Singleton**
- **Factory**
- **Repository**
- **Unit of Work**
- **CQRS**

### Архитектура
- **Чистая архитектура**: Разделение на слои (Domain, Application, Infrastructure, Presentation).
- **DDD (Domain-Driven Design)**: Моделирование бизнес-логики на основе предметной области.

### Методы решения
- **KD-Tree**: Для оптимизации поиска.

## Микросервис Telegram Bot (Progress 60%)

Telegram Bot (telegram_bot/) — это сервис для взаимодействия с Telegram API, реализующий админ-панель для управления пользователями и конфигурацией этапов сборки приложений.

## Технологии
- **Python 3.12**
- **Aiogram 3+**
- **Httpx**
- **MongoDB**
- **Docker**

## Паттерны
- **Factory**
- **Singleton**
- **Repository**
- **Result**
- **Unit of Work**
- **Dependency Injection**
