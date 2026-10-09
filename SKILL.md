---
name: firefox-agent-browser
description: Strict operational skill for AI agent working with Firefox/Camoufox MCP browser tools. Enforces state-first video control, prevents hallucination, and blocks direct JS manipulation of media players.
---

# FULL COMBINED SKILL: Firefox Browser Agent Strict Operating Procedure v4

## 0. Назначение и принудительная активация / Purpose & Activation
Этот скилл обязателен при любой работе с браузерным MCP-инструментом Firefox/Camoufox.

**EN:** This skill is mandatory when working with the Firefox/Camoufox MCP browser tool. It enforces strict state-first operations and prevents hallucination.

### 0.1. Условия активации / Activation Triggers
Активировать режим **Browser Strict Mode** немедленно, если в контексте появляется любой инструмент, имя которого содержит: `browser_`, `video_`, `firefox`, `camoufox`.

### 0.2. Иерархия приоритетов / Priority Hierarchy
Безопасность > Точность > Скорость > Универсальность.

### 0.3. Компактные правила Strict Mode / Compact Rules
- Все действия ТОЛЬКО через `browser_*` и `video_*` инструменты.
- Никогда не конструировать URL поиска вручную.
- Не утверждать успех без подтверждения от инструмента.
- При изменении fingerprint — немедленная остановка серии.

---

## 1. ГЛАВНЫЕ ЗАПРЕТЫ / CRITICAL PROHIBITIONS (MUST NEVER DO)

Агенту КАТЕГОРИЧЕСКИ запрещено / The agent MUST NEVER:

1. **КОНСТРУИРОВАТЬ URL ПОИСКА ВРУЧНУЮ.** Запрещено формировать `youtube.com/results?search_query=...` или любые URL с параметрами поиска. Используйте ТОЛЬКО `browser_search_on_page(query)` для поиска внутри сайта.
   **EN:** NEVER construct search URLs manually. Use `browser_search_on_page(query)` instead.

2. **ИСПОЛЬЗОВАТЬ ПЛЕЙСХОЛДЕРЫ В URL.** Запрещено передавать в `browser_navigate` значения вида `?v=video_id`, `{id}`, `YOUR_ID`, `example_id`. Если вы не знаете реальный URL — сначала найдите его через `browser_find_link()` или `browser_navigate_smart()`.
   **EN:** NEVER use placeholder values in URLs like `video_id`, `{id}`, `YOUR_ID`. Find the real URL first via `browser_find_link()` or `browser_navigate_smart()`.

3. **ИСПОЛЬЗОВАТЬ `browser_evaluate` ДЛЯ УПРАВЛЕНИЯ ВИДЕО.** Запрещены `document.querySelector('video').play()`, `.currentTime = ...`, `.click()` по кнопкам плеера. Только `video_action()`.
   **EN:** NEVER use `browser_evaluate` to control video playback. Only use `video_action()`.

4. **ПОКИДАТЬ СТРАНИЦУ БЕЗ ЯВНОГО ЗАПРОСА.** Если пользователь просит "найди видео X" находясь на YouTube — используйте `browser_search_on_page("X")` или `browser_find_link("X")`, а НЕ `browser_navigate` на другой URL.
   **EN:** Do NOT leave the current page unless explicitly asked. Use `browser_search_on_page()` or `browser_find_link()` to search within the page.

5. **ИГНОРИРОВАТЬ ИЗМЕНЕНИЕ FINGERPRINT.** Если `video_action` вернул `⚠️ Page/video fingerprint changed` — НЕМЕДЛЕННО ОСТАНОВИТЬ серию действий, сообщить пользователю.
   **EN:** If fingerprint changed — STOP immediately, notify user.

6. **ИСПОЛЬЗОВАТЬ TOGGLE ВСЛЕПУЮ.** Для видео: `theater_on` / `theater_off`, `subtitles_on` / `subtitles_off`. Не использовать toggle, если нужно конкретное состояние.
   **EN:** Always use explicit on/off states, not blind toggles.

7. **КЛИКАТЬ ПО ЭЛЕМЕНТАМ ЧЕРЕЗ CSS-СЕЛЕКТОРЫ, ЕСЛИ НЕ ЗНАЕТЕ ТОЧНЫЙ СЕЛЕКТОР.** Используйте `browser_click_text("текст кнопки")` вместо угадывания `[aria-label="..."]`.
   **EN:** Use `browser_click_text("button text")` instead of guessing CSS selectors.

8. **ОСТАВЛЯТЬ ОТКРЫТЫМ МЕНЮ ПЛЕЕРА.** После `quality_set` или любых действий с настройками — вызвать `video_action("cleanup", "menu")`.
   **EN:** Always call `video_action("cleanup", "menu")` after interacting with player menus.

9. **ПОВТОРЯТЬ ОДНО ДЕЙСТВИЕ БОЛЕЕ 2 РАЗ.** Если 2 попытки не удались — СТОП, сообщить пользователю.
   **EN:** Max 2 retries per action. Then STOP and report.

10. **АВТОМАТИЧЕСКИ РЕШАТЬ CAPTCHA, PAYWALL, ПЛАТЕЖИ.** Стоп и запрос ручного вмешательства.
    **EN:** Never auto-solve CAPTCHAs or bypass paywalls. Stop and ask user.

---

## 2. ИНСТРУМЕНТЫ НАВИГАЦИИ И ПОИСКА / Navigation & Search Tools

### `browser_open(target)`
Открытие сайтов. Принимает URL (`youtube.com`), алиас (`ютуб`), или текст ссылки на текущей странице.
**EN:** Opens sites by URL, alias, or link text on current page.

### `browser_navigate(url)`
**ТОЛЬКО для полных, проверенных URL.** Запрещено передавать текстовые запросы или конструировать поисковые URL. Сервер автоматически заблокирует плейсхолдеры.
**EN:** ONLY for complete, verified URLs. Placeholders are blocked by server.

### `browser_search_on_page(query)` ⭐ НОВЫЙ / NEW
Вводит поисковый запрос во встроенное поле поиска страницы и нажимает Enter. **Не меняет URL напрямую**, использует нативный поиск сайта (YouTube, Twitch и т.д.).
Используйте ВМЕСТО ручного конструирования URL поиска.
**EN:** Types query into the page's native search box and presses Enter. Does NOT navigate away. Use INSTEAD of constructing search URLs.

### `browser_find_link(query)`
Ищет ссылки, табы, кнопки и интерактивные элементы на странице по подстроке. Работает с JS-табами (кнопки "Недавно опубликованные", "Новое для вас" и т.д.), aria-labels кнопок (Like/Dislike).
**EN:** Finds links, tabs, buttons, and interactive elements by substring match. Works with JS tabs and aria-labels.

### `browser_navigate_smart(query)`
Fuzzy-поиск по ссылкам на текущей странице. Поддерживает сокращённые запросы ("приказ мосты" найдёт "Приказ уничтожить все мосты"). Если 1 совпадение — переход. Несколько — список для выбора.
**EN:** Fuzzy search over page links. Supports abbreviated queries. 1 match → navigate. Multiple → list for user choice.

### `browser_click_text(text_query, tag="*")` ⭐ НОВЫЙ / NEW
Находит и кликает ЛЮБОЙ видимый элемент (кнопку, таб, ссылку, иконку) по частичному совпадению текста или aria-label. Регистронезависимый, игнорирует пунктуацию.
Примеры:
- `browser_click_text("Нравится")` — кликнет кнопку Like на YouTube
- `browser_click_text("Недавно опубликованные")` — кликнет JS-таб
- `browser_click_text("Подписаться")` — кликнет Subscribe
Если найдено несколько элементов — вернёт нумерованный список.
**EN:** Finds and clicks any visible element by partial text/aria-label match. Case-insensitive. Returns numbered list if multiple matches.

### `browser_snapshot()`
Возвращает JSON: URL, title, player info, video state, fingerprint. Обязателен перед серией видео-действий.
**EN:** Returns page snapshot with fingerprint. Mandatory before video action series.

---

## 3. РАБОТА С СОКРАЩЁННЫМИ ЗАПРОСАМИ / Abbreviated Query Handling

Пользователь МОЖЕТ давать неполные названия видео, ссылок, кнопок. Агент ДОЛЖЕН:

1. **НЕ требовать точного названия.** "приказ мосты" = "Приказ уничтожить все мосты - снос переправ..."
2. **Использовать `browser_navigate_smart()`** для поиска — он применяет token-subset matching (все слова запроса ищутся в тексте независимо от порядка и длины).
3. **Использовать `browser_find_link()`** для обнаружения элементов — он ищет по подстроке в textContent и aria-label.
4. **Использовать `browser_click_text()`** для клика по кнопкам — достаточно части текста.
5. **Игнорировать регистр, пунктуацию, стоп-слова** при формулировании запроса к инструментам.

**EN:** User MAY provide abbreviated names. Agent MUST use fuzzy/partial matching tools. "order bridges" should find "Order to destroy all bridges". Ignore case, punctuation, stop words.

---

## 4. РАБОТА С JS TABS / JavaScript Tab Navigation

JS-табы (фильтры на YouTube: "Все", "Музыка", "Недавно опубликованные", "Новое для вас") — это НЕ ссылки `<a href>`. Это `<div role="tab">`, `<yt-tab-shape>` или `<button>`.

**Правила работы с табами:**
1. **НЕ пытаться** найти их через CSS-селекторы `[href="/feed/uploads"]` — у них нет href.
2. **ИСПОЛЬЗОВАТЬ** `browser_click_text("Недавно опубликованные")` — этот инструмент ищет по textContent и aria-label среди всех интерактивных элементов.
3. **ИСПОЛЬЗОВАТЬ** `browser_find_link("Недавно")` — обновлённый find_link теперь включает табы и кнопки.
4. **ПОСЛЕ клика** обязательно вызвать `browser_get_content()` чтобы проверить, изменился ли контент. JS-табы переключают контент без смены URL!
5. **НЕ утверждать** что таб переключился, пока не проверили контент через `browser_get_content()`.

**EN:** JS tabs have no href. Use `browser_click_text("Tab Name")` to click them. ALWAYS verify content changed via `browser_get_content()` after clicking a tab.

---

## 5. ВИДЕО: ОБЯЗАТЕЛЬНЫЙ ПРОТОКОЛ / Video Control Protocol

### 5.1. Инициализация
Перед любой серией видео-действий: `video_check()` → получить player type, fingerprint, capabilities, state.

### 5.2. Fingerprint Gate
Если fingerprint изменился (автоплей сменил видео) → **НЕМЕДЛЕННАЯ ОСТАНОВКА**. Сообщить пользователю.

### 5.3. Идемпотентность
Всегда явные состояния: `theater_on` / `theater_off`, `subtitles_on` / `subtitles_off`.

### 5.4. Команды `video_action(action, param)`

| Действие / Action | Param | Описание / Description |
|---|---|---|
| `play` | — | Воспроизведение / Play |
| `pause` | — | Пауза / Pause |
| `stop` | — | Стоп (сброс на 0:00) / Stop |
| `restart` | — | Перезапуск / Restart |
| `volume_set` | `0.8` или `80%` | Установить громкость / Set volume |
| `volume_up` | шаг (default 0.1) | Громче / Volume up |
| `volume_down` | шаг (default 0.1) | Тише / Volume down |
| `mute` | — | Mute toggle |
| `seek_forward` | секунды (default 10) | Вперёд / Forward |
| `seek_backward` | секунды (default 10) | Назад / Backward |
| `seek_to` | абсолютные секунды | К позиции / Seek to position |
| `jump_minutes` | минуты | Прыжок на N минут / Jump N minutes |
| `fullscreen_enter` | — | Полный экран вкл / Fullscreen on |
| `fullscreen_exit` | — | Полный экран выкл / Fullscreen off |
| `theater_on` | — | Театральный вкл (клавиша T на YT) / Theater on |
| `theater_off` | — | Театральный выкл / Theater off |
| `wide_mode` | — | Широкий формат (YT .ytp-size-button) / Wide mode |
| `default_size` | — | Обычный размер / Default size |
| `subtitles_on/off/toggle` | — | Субтитры / Subtitles |
| `quality_set` | `1080p`, `720p`, `best`, `auto` | Качество. **Исключает Premium** если не запрошен явно / Quality. Excludes Premium unless requested |
| `set_speed` | `1.5`, `2.0` | Скорость воспроизведения / Playback rate |
| `cleanup` | `menu` | Закрыть меню + сброс фокуса / Close menu + reset focus |
| `state` | — | JSON состояния / State dump |
| `download_source` | — | URL исходника видео / Source URL |

### 5.5. Cleanup
После ЛЮБЫХ действий с меню плеера (quality, settings): `video_action("cleanup", "menu")`.

### 5.6. Разница между Theater и Wide
- **Theater Mode** (`theater_on`) — клавиша `t` на YouTube. Видео растягивается на всю ширину страницы, комментарии уходят вниз.
- **Wide Mode** (`wide_mode`) — кнопка `.ytp-size-button` на YouTube. Переключает между default и wide aspect ratio ВНУТРИ плеера.
- Пользователь говорит "широкий экран" → обычно имеет в виду **Theater Mode** (`theater_on`).

---

## 6. ФОРМЫ И ВЗАИМОДЕЙСТВИЕ / Forms & Interaction

### Формы / Forms
1. `browser_analyze_form(selector)` → анализ полей, bot-traps.
2. `browser_fill_form(selector, json)` → заполнение.
3. `browser_submit_form(selector)` → отправка. **ТРЕБОВАТЬ ПОДТВЕРЖДЕНИЯ** для платежей/удалений.

### Кнопки Like / Dislike / Subscribe и подобные
**НЕ ИСПОЛЬЗОВАТЬ** `browser_click("[aria-label='Like']")` — селекторы угадываются неверно.
**ИСПОЛЬЗОВАТЬ** `browser_click_text("Нравится")` или `browser_click_text("Like")` — инструмент сам найдёт элемент по aria-label или тексту.
Для дизлайка: `browser_click_text("Не нравится")` или `browser_click_text("Dislike")`.

**EN:** For Like/Dislike/Subscribe buttons, use `browser_click_text("text")` instead of guessing CSS selectors.

---

## 7. ОБЩИЙ РАБОЧИЙ ЦИКЛ / General Workflow

1. **Определить тип задачи** (открыть сайт, найти видео, управлять плеером, нажать кнопку).
2. **Проверить статус браузера**. Если `Browser not started` → `browser_start`.
3. **Выбрать минимально достаточный инструмент** (см. таблицу ниже).
4. **Выполнить одно действие**.
5. **Проверить результат** через возвращаемое состояние или `browser_snapshot`.
6. **Если подтверждено** → кратко сообщить пользователю фактическое состояние.
7. **Если не подтверждено** → максимум одна корректирующая попытка, затем стоп.

### Таблица выбора инструментов / Tool Selection Matrix

| Задача пользователя | Инструмент | НЕ использовать |
|---|---|---|
| Открыть сайт | `browser_open("youtube")` | `browser_navigate` с конструированием URL |
| Найти видео на сайте | `browser_search_on_page("название")` | `browser_navigate("youtube.com/results?...")` |
| Кликнуть по ссылке на странице | `browser_navigate_smart("текст")` | `browser_click` с угадыванием селектора |
| Кликнуть кнопку (Like, Tab) | `browser_click_text("Нравится")` | `browser_click("[aria-label=...]")` |
| Найти элемент на странице | `browser_find_link("подстрока")` | `browser_evaluate` с сырым JS |
| Управление видео | `video_action("play")` | `browser_evaluate("video.play()")` |
| Проверить состояние | `browser_snapshot()` | Парсинг `browser_get_content()` |

---

## 8. ОБРАБОТКА ОШИБОК / Error Handling

| Ситуация / Situation | Действие / Action |
|---|---|
| `❌ BLOCKED: URL contains placeholder` | Вы использовали шаблонный URL. Найдите реальный через `browser_find_link()` или `browser_navigate_smart()`. |
| `⚠️ No element found` | Попробуйте более короткую подстроку. Используйте `browser_click_text()` вместо `browser_click()`. |
| `⚠️ Page/video fingerprint changed` | **СТОП.** Видео сменилось (автоплей). Сообщите пользователю. Запросите подтверждение. |
| `❌ Click error: Timeout` | Элемент не найден по CSS-селектору. Используйте `browser_click_text("текст элемента")`. |
| Меню качества зависло | `video_action("cleanup", "menu")`. Если fullscreen → не нажимать Escape. |
| > 2 неудач подряд | **СТОП.** Суммировать факты, запросить ручное вмешательство. |
| CAPTCHA / Paywall | **НЕМЕДЛЕННЫЙ СТОП.** Попросить пользователя решить вручную. |

---

## 9. ФОРМАТ ОТВЕТОВ / Response Format

### После успешного действия / After success
> Выполнено: {действие}. Состояние: {ключевые параметры}.
> EN: Done: {action}. State: {key parameters}.

**Запрещено** выводить цепочки рассуждений, попытки, ошибки прошлых шагов. Только итоговый факт.
**EN:** Do NOT output reasoning chains, attempt histories, or past errors. Only final fact.

### После неуверенного результата / Uncertain result
> Действие отправлено, но состояние не подтверждено: {поле}. Требуется проверка.
> EN: Action sent but state not confirmed: {field}. Verification needed.

### При остановке / Emergency stop
> Остановка. Причина: {причина}. Последнее состояние: {state}. Что нужно: {действие}.
> EN: Stopped. Reason: {reason}. Last state: {state}. Needed: {action}.

---

## 10. МИНИМАЛЬНЫЙ ЧЕК-ЛИСТ ПЕРЕД ОТВЕТОМ / Pre-response Checklist

- [ ] Инструмент вернул `✅` или подтверждённое состояние (не `⚠️` и не `❌`).
- [ ] Нет `warning` / `not confirmed` в ответе инструмента.
- [ ] Fingerprint не изменился (если серия видео-действий).
- [ ] `menu_open=false` после любых действий с настройками плеера.
- [ ] Фокус/мышь сброшены (выполнен cleanup).
- [ ] **Не использовались** плейсхолдерные URL (`video_id`, `{id}`).
- [ ] **Не конструировались** поисковые URL вручную.
- [ ] **Не использовался** `browser_evaluate` для управления видео.
- [ ] Ответ содержит только фактическое состояние, без рассуждений.
- [ ] Для кнопок (Like, табы) использован `browser_click_text()`, а не угадывание CSS.