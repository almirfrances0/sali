---
name: Laravel
tags: [laravel, php, framework, eloquent, blade, artisan, mvc]
dependencies: [php]
version_hint: laravel@11
project_detect:
  - composer.json
  - artisan
  - app/Http/Controllers
  - routes/web.php
summary: Laravel is convention-driven PHP with a rich stdlib. Prefer Laravel-native (queue for async, cache for state, event/listener for cross-cutting, jobs for background work) over hand-rolled infrastructure. Eloquent for common queries, raw SQL for complex ones. Sanctum for API auth (SPA + mobile). Blade for server-rendered views, Inertia for SPA-with-server-routing. Tests via Pest or PHPUnit against SQLite in-memory OR a scratch Postgres. Never mass-assign without $fillable / $guarded. Never `$request->all()` into a model without validation.
---

# Laravel (production)

## Version detection

`composer.json` → `require."laravel/framework"`. Common: 10.x, 11.x, 12.x. The API surface is
stable but conventions shift (11 dropped `app/Http/Middleware`, moved config to
`bootstrap/app.php`; do NOT copy 10-era guidance into an 11 project).

## Structure (Laravel 11+)

    app/
      Http/
        Controllers/     # thin — validate, dispatch, return
        Requests/        # FormRequest classes — validation + authorization
      Models/            # Eloquent
      Services/          # domain logic (a controller calls services)
      Actions/           # single-purpose invokables (`CreateUser::handle`)
      Jobs/              # queueable work
      Events/, Listeners/
      Notifications/
    routes/
      web.php, api.php
    database/
      migrations/, seeders/, factories/
    config/
    tests/               # Pest (default 10+) or PHPUnit

Thin controllers, fat services. A controller that's more than 20 lines is doing too much;
extract to a service or action.

## Routing

    // routes/api.php
    Route::middleware(['auth:sanctum'])->group(function () {
        Route::apiResource('orders', OrderController::class);
        Route::post('orders/{order}/cancel', [OrderController::class, 'cancel']);
    });

* Route-model binding (`{order}` → `Order` instance) — for free with matching parameter name.
* `apiResource` gives you index/store/show/update/destroy without noise.
* Middleware in groups, not per-route (unless truly per-route).

## Validation via FormRequest

    class StoreOrderRequest extends FormRequest {
        public function authorize(): bool { return $this->user()->can('create', Order::class); }
        public function rules(): array {
            return ['product_id' => 'required|uuid|exists:products,id',
                    'quantity'   => 'required|integer|min:1|max:1000'];
        }
    }

    // Controller:
    public function store(StoreOrderRequest $request) {
        $data = $request->validated();
        return CreateOrder::run($data);   // action class
    }

* NEVER `$request->all()` into a model — mass-assignment vulnerability.
* `authorize()` returns bool; `false` = 403. Combine with Gates/Policies for complex cases.

## Eloquent

* `Order::find($id)` for PK lookup.
* `Order::where('status', 'open')->latest()->limit(20)->get()` — chainable, readable.
* `Order::with(['product', 'customer'])->get()` — eager-load to avoid N+1.
* `$order->refresh()` reloads from DB — useful after an update.
* `$order->update($data)` inside a transaction if consistency across rows matters.

Beyond simple lookups, use raw SQL — Eloquent's aggregates are limited. `DB::table('orders')
->selectRaw('DATE(created_at) as day, count(*) as n')->groupBy('day')->get()`.

## Migrations

    Schema::create('orders', function (Blueprint $t) {
        $t->uuid('id')->primary();
        $t->foreignUuid('user_id')->constrained();
        $t->string('status')->default('open')->index();
        $t->decimal('total_cents', 15, 0);
        $t->timestamps();
    });

* Always down-migrate — every `up()` gets a mirror `down()`.
* `php artisan migrate` in dev. In prod: `php artisan migrate --force` in the deploy script.
* Never edit an old migration — write a new one.
* Foreign keys with `constrained()` + `->cascadeOnDelete()` or `->restrictOnDelete()` per business
  rule.

## Auth (Sanctum for API)

    // config/sanctum.php — stateful for SPAs (same domain), token for mobile
    'stateful' => explode(',', env('SANCTUM_STATEFUL_DOMAINS')),

* SPA on same origin: `Auth::guard('web')` + `csrf-cookie` route. Session-based.
* Mobile / cross-origin: personal-access-tokens (`$user->createToken('device')`). Bearer.
* API rate-limit: `Route::middleware('throttle:60,1')` per user per minute.

## Queues + jobs

    // app/Jobs/SendWelcomeEmail.php — implements ShouldQueue
    class SendWelcomeEmail implements ShouldQueue {
        public function __construct(public User $user) {}
        public function handle(): void {
            Mail::to($this->user)->send(new WelcomeMail($this->user));
        }
    }

    SendWelcomeEmail::dispatch($user);   // enqueued, returns immediately

* Anything I/O-heavy in a request handler is a JOB. Send an email? Job. Call an external API? Job.
* Queue driver: `redis` (recommended), `database` (fine for low volume), `sync` (dev only).
* Workers: `php artisan queue:work` via supervisor / systemd. `--tries=3 --backoff=10` for
  retries.
* Failed jobs go to `failed_jobs` table; retry via `queue:retry`.

## Events + listeners

* Fire an event when a domain change happens (`OrderCreated`). Listeners react (send
  notification, update stats). Loose coupling — controllers don't know about listeners.
* Listeners should be QUEUEABLE (`implements ShouldQueue`) unless they must run sync.

## Testing

    // tests/Feature/OrderTest.php (Pest)
    it('creates an order', function () {
        $user = User::factory()->create();
        actingAs($user)->postJson('/api/orders', ['product_id' => Product::factory()->create()->id,
                                                   'quantity' => 3])
            ->assertStatus(201);
        expect(Order::count())->toBe(1);
    });

* `use RefreshDatabase;` for fresh schema per test.
* Factories with sensible defaults + explicit overrides.
* `actingAs($user)` for authenticated tests.
* `$this->getJson()` / `postJson()` for API tests.

## Anti-patterns

* Business logic in controllers — extract to services / actions.
* Business logic in Eloquent models — models are DATA, not behaviour.
* `env()` outside `config/` — Laravel caches config in prod; `env()` returns null.
* Storing files in `public/` — use `Storage::disk('public')->put()`; symlink via `storage:link`.
* Using session in an API controller — APIs are stateless.
* `$request->all()` mass-assignment — always `$request->validated()` or `only([...])`.
* Ignoring `n+1` — always `with([...])` when the view iterates related rows.
* Editing an existing migration — write a new one.

## Performance

* Cache: `Cache::remember('expensive.key', 60, fn() => …)` — 60 s.
* Query cache: `$q->rememberFor(60)` (spatie/laravel-query-builder or the `laravel-remember-me`
  packages).
* Eager loading: watch for N+1 with Debugbar or Telescope.
* Compile assets: `php artisan config:cache && php artisan route:cache && php artisan view:cache`
  in deploy.
