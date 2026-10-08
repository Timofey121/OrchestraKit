# Участие в OrchestraKit

Начните с небольшой задачи или issue. Найдите существующий тест, который описывает близкое
поведение, затем добавьте или измените `unittest` для нового поведения. Для
изменений runtime тест должен падать до реализации.

## Локальная проверка

Запускайте из корня репозитория:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
git diff --check
```

Изменения панели также требуют Node.js и её проверок:

```bash
cd frontend
npm ci --ignore-scripts
npm run build
node --test src/*.test.js
```

Проверяйте созданный пакет, если меняете установку, ресурсы или манифест:

```bash
python3 -m pip install build
python3 -m build
```

## Карта кода

| Задача | Основные файлы |
|---|---|
| CLI и подключение проекта | `src/orchestra_kit/cli.py`, `project.py`, `install.py`, `render.py` |
| Конфигурация, профили и маршрутизация | `config.py`, `routing.py`, `model_catalog.py`, `templates/project.toml` |
| Нативный листовой запуск | `execution.py`, `check_execution.py`, `log_capture.py`, `contracts.py`, `isolation.py` |
| Задачи, evidence и история | `state.py`, `evidence.py`, `events.py`, `fingerprint.py`, `handoff.py` |
| Очередь и leases | `queue.py` |
| Учёт использования и калибровка | `usage.py`, `calibration.py` |
| Внешние Providers и секреты | `providers.py`, `settings.py` |
| Панель | `dashboard.py`, `src/orchestra_kit/web/`, `frontend/` |
| Пользовательский навык и роли | `skills/orchestra/`, `templates/`, `.agents/skills/` в подключённом проекте |

Тесты повторяют эту структуру в `tests/`. Генерируемые проектные файлы не
редактируют вручную: измените шаблон или рендеринг и проверьте `init`, `sync` и
`doctor`.

## Расширение конфигурации

Для нового Responses-совместимого провайдера добавьте запись в проектный
`project.toml`, затем назначьте её профилю:

```toml
[providers.example]
name = "Example"
base_url = "https://api.example.com/"
env_key = "EXAMPLE_API_KEY"
wire_api = "responses"
model_catalog_json = ".orchestra/providers/example-models.json"
capabilities = ["function", "apply_patch"]

[profiles.balanced]
model = "example-model-id"
effort = "high"
provider = "example"
```

Не добавляйте значение ключа в TOML, тестовые данные или документацию. Проверьте
конфигурацию `sync` и `doctor`; реальный платный запрос не является частью
стандартной проверки этого репозитория.

При добавлении роли меняйте шаблон конфигурации, проектный рендеринг, контракт
исполнителя и тесты вместе. Листовой агент остаётся исполнителем ограниченного
задания и не делегирует работу.

## Issue и pull request

Создавайте issue через шаблон bug report или feature request. Укажите версию,
платформу, шаги воспроизведения и безопасные фрагменты локального вывода. Не
включайте API-ключи, содержимое личных чатов или полный домашний путь, если без
него можно обойтись.

В pull request опишите проблему, итоговое поведение, изменённые файлы и команды
проверки с их результатом. Привяжите issue, если он есть. Не включайте `dist/`, `node_modules/`, локальные журналы, историю запусков из
`.orchestra/` или IDE-метаданные. Конфигурация `.orchestra/project.toml` и
управляемые инструкции могут храниться в Git.
Используйте [шаблон PR](.github/pull_request_template.md).

## Документация

README и основные руководства написаны по-русски. Отдельные справочники могут
быть на английском. Документация описывает текущий код. Не обещайте доступность
модели, стоимость, поддержку любого API или поведение хоста, которое не
проверяется этим проектом. Сохраняйте команды, идентификаторы и ссылки при
редактировании текста.

## Автоматическая проверка в GitHub

Workflow `Checks` запускает Python-тесты, тесты и сборку панели, проверяет
актуальность готового `app.js` и собирает дистрибутивы. В матрице заданы Linux
и macOS с Python 3.11 и 3.13. Он не требует ключей моделей и не делает платных
запросов. Первый запуск на GitHub произойдёт после публикации репозитория.

Используются официальные [checkout](https://github.com/actions/checkout),
[setup-python](https://github.com/actions/setup-python) и
[setup-node](https://github.com/actions/setup-node).
