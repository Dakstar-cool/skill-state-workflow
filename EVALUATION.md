# Оценка SKILL.state Workflow

Дата: 2026-09-01

## Итог

Персональный `skill-state-workflow` обновлён до трёхуровневой схемы progressive disclosure и признан независимым аудитом готовым к использованию.

- Любой project-чат включает компактный `SKILL.md` с первого сообщения.
- Риск восстановления, ожидания, повторной ошибки, handoff или параллельного canonical state подключает `long-horizon.md`.
- Персистентный JSON и детерминированный guard подключаются только когда state должен пережить compaction, restart, handoff, extended wait или risky recovery.
- Обычный одноразовый Projectless-чат остаётся без skill.

## Что добавлено

В `scripts/state_guard.py` реализован standalone guard только на стандартной библиотеке Python:

- строгий UTF-8 JSON без BOM, duplicate keys, `NaN`/`Infinity`, `null`-значений, лишних envelope-полей и чрезмерной глубины;
- плоский versioned envelope с `task_id`, `owner`, `objective_revision`, `state_revision` и evidence текущей ревизии;
- compare-and-swap по ожидаемой ревизии внутри постоянного sidecar-lock;
- единственный canonical writer и атомарный `handoff` владельца;
- recursive map patch, замена list/scalar, `null` как delete-sentinel и запрет неявного scalar-to-object reinterpretation;
- candidate-first validation: отклонённая мутация оставляет state побайтно неизменным;
- no-op без rewrite, timestamp change и revision bump;
- same-directory temp, `flush/fsync`, `os.replace`, POSIX directory fsync и отдельный `COMMIT_UNCERTAIN` после неоднозначного commit;
- trusted-root, symlink/reparse/hard-link checks и стандартный лимит 32 KiB;
- машинно-читаемые JSON-результаты для `init`, `check`, `apply` и `handoff`.

Смена objective, ID критерия или его формулировки теперь требует следующую `objective_revision`. Старое `passed`/evidence нельзя сохранить для изменённого требования. Внешние эффекты намеренно не входят в файловую транзакцию: неопределённый результат сначала сверяется read-only.

## Проверки

| Область | Результат |
|---|---:|
| Skill format validator | PASS |
| Startup, instruction, isolation, recovery и guard-инварианты | 52/52 |
| Persistent guard regressions | 26/26 |
| Прежний in-memory runtime | 8/8 |
| Независимые routing-сценарии L0–L3 | 22/22 |
| Независимые гонки двух writers | 10/10 |
| Adversarial security/correctness review | SHIP-READY |

Проверены CAS lost-update, cross-process lock timeout, wrong task/owner, owner handoff, nested merge/delete, stale evidence, изменение требований, no-op, UTF-8 byte cap, malformed schema types, duplicate JSON, excessive nesting, hard links, trusted root, сбой `os.replace`, неопределённый post-commit fsync и сбой release-lock. Все отклонённые изменения сохраняют прежние байты state.

Аудит нашёл и до выпуска закрыл три контрпримера: reuse старого `passed` после изменения формулировки критерия, сырой `TypeError` на status неправильного типа и немашинный сбой release-lock после commit.

## Routing и progressive disclosure

Независимая матрица из 22 сценариев дала 22 однозначных решения и 0 противоречий:

| Уровень | Когда | Сценариев |
|---|---|---:|
| L0 | Обычный Projectless one-shot | 2 |
| L1 | Project bootstrap или явно long-running/resumable Projectless | 6 |
| L2 | Wait/retry/recovery, uncertain effect или parallel canonical facts | 6 |
| L3 | Durable boundary: extended wait, compaction/restart, handoff, persistent recovery | 8 |

`long-running/resumable` означает, что незавершённый state должен пережить текущий turn/session или заранее нужен recovery point. `Extended wait` должен с высокой вероятностью пережить текущий turn/context. Evidence прежней objective revision сбрасывается или понижается уже на L1 и строго отклоняется guard на L3.

## Масштабирование на 1 000 шагов

Синтетический append-only baseline вырос с 84 до 78 005 символов на prompt и накопил 39 044 001 символ. State runtime оставался на 485 символах и накопил 485 000 — снижение 80,5x без учёта инструкций.

Консервативный расчёт повторно учитывает загруженный Markdown на каждом шаге:

| Путь | Instruction chars | Снижение за 1 000 шагов | Точка окупаемости |
|---|---:|---:|---:|
| L1: `SKILL.md` | 1 749 | 17,48x | ~57 шаг |
| L2: + `long-horizon.md` | 5 392 | 6,64x | ~150 шаг |
| L3: + `persistent-state.md` | 11 060 | 3,38x | ~295 шаг |

Python guard не включён в instruction-char метрику: он выполняется как детерминированный скрипт, тогда как в model context загружаются только необходимые Markdown-инструкции. Цена L3 выше, но она возникает только там, где потеря или гонка state существенно дороже дополнительного контекста.

## Ограничения

- Это детерминированные и adversarial тесты, а не полный black-box A/B на большом наборе свежих задач Codex.
- Host всё ещё физически хранит историю; полностью идентичная статье архитектура требует отдельного runtime, формирующего только `(skill, state, latest observation)`.
- Lock и atomic rename рассчитаны на cooperating writers и доверенный локальный workspace, не на hostile same-account процессы, SMB/NFS/FUSE.
- Standard-library Python на Windows не гарантирует directory flush при внезапном отключении питания.
- Guard защищает state, lock и temp; пользовательский patch-файл должен иметь подходящие workspace ACL.
- Внешние API/файловые эффекты не откатываются вместе с state и требуют read-only reconciliation при неопределённости.

Основа подхода: [SKILL.state: Scalable Long-Horizon Agent Skills](https://arxiv.org/html/2608.26263v2). Формат skill, optional scripts и progressive disclosure сверены с [официальной документацией OpenAI](https://learn.chatgpt.com/docs/build-skills).
