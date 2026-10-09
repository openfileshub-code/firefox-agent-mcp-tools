# firefox-agent-mcp-tools

---

## 🇷🇺 О ПРОЕКТЕ (русский)

### ⚠️ ВАЖНОЕ УВЕДОМЛЕНИЕ О СТАТУСЕ ПРОЕКТА

**Этот проект находится в стадии РАННЕГО предварительного тестирования и является пока довольно сырым (raw/prototype stage).**

✅ Проект предоставляет функционал для управления браузером Firefox через MCP (Model Context Protocol) интерфейс с использованием Camoufox.

❌ **НЕ ИСПОЛЬЗУЙТЕ ЭТОТ ПРОЕКТ В ПРОИЗВОДСТВЕННЫХ СРЕДАХ.** Код находится в активной разработке, может содержать баги, неполный функционал или нестабильные реализации.

🔬 Проект предназначен для тестирования, исследования и разработки. Любое использование "как есть" осуществляется на ваш собственный риск.

---

### 📋 Описание проекта

`firefox-agent-mcp-tools` — это набор инструментов MCP (Model Context Protocol) для управления браузером Firefox/Camoufox через ИИ-агентов. Проект предоставляет модульную архитектуру с изолированными компонентами для различных групп функций:

- 🎥 **Управление видеоплеерами** — контроль воспроизведения, качества, субтитров, полноэкранного режима
- 🪟 **Управление окнами** — сохранение и восстановление размеров и позиций окон браузера
- 🔗 **Умная навигация и поиск** — фаззи-поиск по ссылкам, нативный поиск на страницах, обход JS-табов
- 📝 **Заполнение форм** — анализ и заполнение форм на веб-страницах с обнаружением бот-ловушек
- 📸 **Скриншоты и JS-оценка** — получение снимков страницы и выполнение JavaScript-кода

### 🏗️ Архитектура проекта

```
firefox-agent-mcp-tools/
├── server.py                  # Главный файл MCP-сервера с регистрацией инструментов
├── SKILL.md                   # Навык для ИИ-агента (firefox-agent-browser)
├── requirements.txt           # Зависимости проекта
├── .gitignore                # Исключения для Git
├── config/
│   ├── sites.json            # Алиасы сайтов (youtube, twitch и т.д.)
│   └── window.json           # Сохраненные размеры и позиции окон
├── modules/
│   ├── __init__.py           # Регистрация модулей
│   ├── video_controller.py   # Управление видеоплеерами
│   ├── window_manager.py     # Управление окнами
│   └── link_cache.py         # Кэш ссылок + фаззи-поиск
├── playwright_profile/        # Персистентный профиль браузера
└── mcp_debug.log              # Лог работы сервера
```

### 🔧 Особенности

- **Модульная архитектура** — каждая группа функций управляется флагом `ENABLE_*` в `server.py`
- **Отключаемое логирование** — конфигурация логирования через `ENABLE_LOGGING` и `LOG_LEVEL`
- **Защита от галлюцинаций ИИ** — блокировка навигации по плейсхолдер-URL (например, `video_id`, `{id}`)
- **Строгий режим работы с видео** — запрет прямого JS-вмешательства в плееры, использование только `video_action()`
- **Постоянный профиль браузера** — сохранение сессий между запусками через `persistent_context=True`

---

## 🇬🇧 PROJECT DESCRIPTION (english)

### ⚠️ IMPORTANT PROJECT STATUS NOTICE

**This project is in EARLY preliminary testing stage and is considered RAW/PROTOTYPE quality.**

✅ The project provides functionality for managing Firefox browser through MCP (Model Context Protocol) interface using Camoufox.

❌ **DO NOT USE THIS PROJECT IN PRODUCTION ENVIRONMENTS.** The code is under active development and may contain bugs, incomplete functionality, or unstable implementations.

🔬 The project is intended for testing, research, and development purposes. Any use "as is" is at your own risk.

---

### 📋 Project Description

`firefox-agent-mcp-tools` is a set of MCP (Model Context Protocol) tools for managing Firefox/Camoufox browser through AI agents. The project provides a modular architecture with isolated components for various feature groups:

- 🎥 **Video Player Control** — playback control, quality settings, subtitles, theater mode
- 🪟 **Window Management** — save and restore browser window size and position
- 🔗 **Smart Navigation & Search** — fuzzy link search, native page search, JS-tab handling
- 📝 **Form Tools** — form analysis and filling with bot-trap detection
- 📸 **Screenshot & JS Evaluate** — page snapshots and JavaScript execution

### 🏗️ Project Architecture

```
firefox-agent-mcp-tools/
├── server.py                  # Main MCP server file with tool registration
├── SKILL.md                   # AI agent skill (firefox-agent-browser)
├── requirements.txt           # Project dependencies
├── .gitignore                # Git exclusions
├── config/
│   ├── sites.json            # Site aliases (youtube, twitch, etc.)
│   └── window.json           # Saved window sizes and positions
├── modules/
│   ├── __init__.py           # Module registration
│   ├── video_controller.py   # Video player control
│   ├── window_manager.py     # Window management
│   └── link_cache.py         # Link cache + fuzzy search
├── playwright_profile/        # Persistent browser profile
└── mcp_debug.log              # Server log file
```

### 🔧 Features

- **Modular architecture** — each feature group is managed by an `ENABLE_*` flag in `server.py`
- **Toggleable logging** — logging configuration via `ENABLE_LOGGING` and `LOG_LEVEL`
- **AI hallucination protection** — blocks navigation to placeholder URLs (e.g., `video_id`, `{id}`)
- **Strict video control mode** — prohibits direct JS interference with players, uses only `video_action()`
- **Persistent browser profile** — session preservation between launches via `persistent_context=True`

---

### 🚀 Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run server
python server.py
```

### 📄 License

This project is provided "as is" for testing and development purposes.
