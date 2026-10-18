# СЕД ПУЕТ у GitHub Codespaces

Стек запускається автоматично (`setup.sh` після створення, `start.sh` при кожному старті codespace).
Коли в терміналі з'явиться «СЕД ПУЕТ запущено», відкрийте вкладку **Ports** → порт **8080** (значок глобуса).
Якщо браузер показує HTTP ERROR 401 — правою кнопкою на 8080 → Port Visibility → Public (на час показу; потім поверніть Private).

* Вхід: «Увійти через SSO ПУЕТ», логін `rector`, `vicerector`, `director`, `head.kknit`, `lecturer`, `student`,
  `studgov`, `buh`, `pfv` або `clerk`, пароль `demo`.
* Адмінка Keycloak: `<адреса 8080>/auth/admin`, логін `admin`, пароль — `KEYCLOAK_ADMIN_PASSWORD` у `.env`.
* Стан сервісів: `docker compose ps`; журнали: `docker compose logs -f app`.
* Перезапуск: `bash .devcontainer/start.sh`. Індексація нормативного корпусу: `.devcontainer/corpus-index.log`.

LLM — `gemma3:4b` на процесорі (Ollama), тому відповіді ШІ повільніші й менш точні, ніж на сервері з GPU.
Докладніше: розділ «Демо в GitHub Codespaces» у [README](../README.md).
