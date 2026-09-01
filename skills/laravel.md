---
name: Laravel Development
tags:
  - laravel
  - php
  - artisan
  - eloquent
  - blade
  - composer
  - phpunit
---

# Laravel Development

Guidance for building and maintaining Laravel applications. This is task guidance, not a rulebook — follow the workspace and safety policy above it.

## Scaffolding
- New app: `composer create-project laravel/laravel .` inside the task workspace (note the trailing `.` — create in the workspace, never a new sibling folder).
- Confirm `php artisan --version` before continuing.
- Copy `.env.example` to `.env`, then `php artisan key:generate`.

## Database
- Configure `.env` (`DB_CONNECTION`, `DB_DATABASE`, …) before migrating.
- Migrations live in `database/migrations`; run `php artisan migrate`. Never edit an applied migration — add a new one.
- Models: `php artisan make:model Post -mfc` (model + migration + factory + controller).

## Auth & admin
- Prefer Laravel Breeze/Fortify for auth: `composer require laravel/breeze --dev && php artisan breeze:install`.
- Admin panels: gate routes with middleware (`auth`, a role/`can` check). Never rely on hiding UI alone.

## Front end
- Blade views in `resources/views`; components with `php artisan make:component`.
- Vite is the default bundler (`npm install && npm run build` / `npm run dev`).

## Verification (what the reviewer will look for)
- Tests pass: `php artisan test` (or `./vendor/bin/phpunit`) — a green run is the evidence a feature works.
- The build succeeds: `npm run build`.
- Migrations applied cleanly and the expected routes exist: `php artisan route:list`.
- Concrete artifacts exist in the workspace (composer.json, the created models/controllers/views).

## Common gotchas
- File permissions on `storage/` and `bootstrap/cache/` must be writable.
- Clear caches after config changes: `php artisan config:clear`, `route:clear`, `view:clear`.
- Version differences change syntax/commands — when unsure of the current Laravel version's API, check the official docs rather than guessing.
