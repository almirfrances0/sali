---
name: PHP
tags:
  - php
  - composer
  - phpunit
  - laravel
---

# PHP

## Environment
- Check the runtime: `php --version`, `composer --version`.
- Extensions matter — install what an app needs (e.g. `php-xml`, `php-mbstring`, `php-pdo`, `php-mysql`/`php-pgsql`) with the system package manager. A "class not found"/"extension missing" error names the extension.
- Dependencies: `composer install` (respects `composer.lock`); `composer require vendor/pkg` to add one.

## Conventions
- PSR-12 style; autoload via Composer PSR-4 (`composer dump-autoload` after adding classes outside the autoloaded paths).
- Prefer typed properties and return types.

## Verification
- Tests: `./vendor/bin/phpunit` or the framework's runner. Green tests are the evidence a change works.
- Lint/static: `./vendor/bin/php-cs-fixer` / `phpstan` if the project configures them.

## Gotchas
- `require` a missing extension → install it, then re-run. Don't mark a step done until the command actually succeeds.
- Composer memory limits: `COMPOSER_MEMORY_LIMIT=-1 composer …` for large installs.
