---
name: Laravel Blade
tags: [laravel, blade, php, html, css, tailwind, frontend, website, template]
dependencies: [php, tailwind]
version_hint: Laravel 11+ / Blade
project_detect:
  - artisan
  - composer.json
  - resources/views
summary: Build Laravel Blade frontend pages. Blade templates with @extends/@section/@yield. Tailwind CSS for styling. Components for reusable UI. Route definitions in web.php. Layout in layouts/app.blade.php. Pages in pages/*.blade.php.
---

# Laravel Blade (production)

## Project Structure

```
app/
  Http/Controllers/PageController.php
resources/
  views/
    layouts/
      app.blade.php          # master layout
    components/
      navbar.blade.php       # reusable nav
      footer.blade.php       # reusable footer
    pages/
      home.blade.php
      about.blade.php
      services.blade.php
      contact.blade.php
  css/
    app.css                  # Tailwind entry
  js/
    app.js                   # JS entry
routes/
  web.php                    # route definitions
```

## Layout Pattern

```blade
{{-- layouts/app.blade.php --}}
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>@yield('title', 'Default Title')</title>
    @vite(['resources/css/app.css', 'resources/js/app.js'])
</head>
<body>
    @include('components.navbar')
    <main>@yield('content')</main>
    @include('components.footer')
</body>
</html>
```

## Page Pattern

```blade
{{-- pages/home.blade.php --}}
@extends('layouts.app')
@section('title', 'Home — Company Name')
@section('content')
<section class="hero">
    <div class="container">
        <h1>Hero Title</h1>
    </div>
</section>
@endsection
```

## Route Pattern

```php
// routes/web.php
Route::view('/', 'pages.home');
Route::view('/about', 'pages.about');
Route::view('/services', 'pages.services');
Route::view('/contact', 'pages.contact');
```

## Rules

- Every page extends `layouts.app` — NEVER duplicate `<head>`, `<nav>`, `<footer>`.
- Navbar and footer are Blade components (`@include` or `<x-navbar>`).
- Tailwind CSS for ALL styling — never inline styles, never separate CSS files.
- Mobile-first responsive design.
- Use `@vite` for asset compilation.
