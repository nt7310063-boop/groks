# GrokFlow

Nền tảng quản lý Chrome Profile, API Key và hàng đợi job tự động hóa cho việc tạo nội dung ảnh/video qua các provider như Grok và Flow. Khách hàng tự đăng nhập tài khoản provider; hệ thống chỉ điều phối profile, queue và lưu kết quả.

> ⭐ **Quy trình code & deploy (làm theo từng bước)**: [docs/QUY-TRINH-DEPLOY.md](docs/QUY-TRINH-DEPLOY.md)
> Phân tích đầy đủ: [docs/answer/phan-tich-du-an-ui-ux-pro-max.md](docs/answer/phan-tich-du-an-ui-ux-pro-max.md)
> Tổng hợp docs: [docs/README.md](docs/README.md)

## Cấu trúc repo

```
GrokFlow/
├── backend/         FastAPI (Python 3.11) – API + worker + browser automation
├── frontend/        React 18 + Vite + TypeScript + Tailwind
├── docs/
│   ├── answer/      Phân tích, kiến trúc, ADR, setup
│   └── api/         Đặc tả REST API (internal + public v1)
├── docker-compose.yml
└── README.md
```

## Quickstart

```powershell
copy backend\.env.example backend\.env
copy frontend\.env.example frontend\.env
docker compose up -d postgres redis
docker compose up backend worker frontend
```

Sau khi backend lên, seed admin:

```powershell
docker compose exec backend python -m app.scripts.create_admin --email admin@local --password ChangeMe123!
```

Truy cập:

- Frontend: http://localhost:5173
- Backend API + Swagger UI: http://localhost:8000/docs

Chi tiết setup: [docs/answer/setup.md](docs/answer/setup.md).

## Stack chính

| Layer | Công nghệ |
|---|---|
| Frontend | React 18, Vite, TypeScript, TailwindCSS, TanStack Query, Zustand, React Hook Form |
| Backend | FastAPI, SQLAlchemy 2 async, Alembic, Pydantic 2 |
| Data | PostgreSQL 16, Redis 7 |
| Worker | Async polling worker (MVP) → RQ/Celery (Phase 4) |
| Browser | Playwright + Chromium (sẽ tích hợp Phase 2) |

## Branching model

Three long-lived branches:

| Branch | Purpose | Direct push? |
|---|---|---|
| `prod` | Production code, deployed to live | ❌ PR only |
| `staging` | QA / demo environment | ❌ PR only |
| `dev` | Integration of all in-progress features | ✅ via PR review |

Working branches:

- `feat/<feature_name>` — new feature, branched from `dev`
- `fix/<bug_name>` — bug fix, branched from `dev`
- `hotfix/<name>` — emergency fix, branched from `prod`

### Standard flow

```
dev
 └── feat/login_page          → PR → dev
                                       └── promote → staging  (QA)
                                                       └── promote → prod  (release)
```

### Hotfix flow

```
prod
 └── hotfix/payment_crash     → PR → prod
                                       ├── back-merge → staging
                                       └── back-merge → dev
```

## Commit convention

Format: `<type>[optional scope]: <description> [#issue_id]`

- ≤ 50 chars, no trailing period, single language per message.
- Reference the issue id when one exists.

| Type | Use for |
|---|---|
| `feat` | new feature |
| `fix` | bug fix |
| `refactor` | code improvement, no behavior change |
| `docs` | documentation only |
| `chore` | minor non-code changes |
| `style` | UI / CSS / formatting |
| `perf` | performance improvement |
| `vendor` | dependency / lockfile bump |

Examples:

```
feat: add login page #12
fix(login): handle empty username #34
vendor(docker-compose): bump redis to latest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for full rules.

<!-- CI/CD Test: 2026-06-10 13:25 -->
<!-- CI/CD Test: 2026-06-10 13:30 -->
