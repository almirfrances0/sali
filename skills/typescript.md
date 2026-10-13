---
name: TypeScript
tags: [typescript, ts, types, generics, strict]
dependencies: [javascript]
version_hint: typescript@5.4+
project_detect:
  - tsconfig.json
summary: TypeScript is JavaScript with a compile-time type system. Turn on strict mode (`"strict": true`) always. Never `any` in new code — use `unknown` + narrow. Prefer type inference; annotate function signatures + public APIs. Use discriminated unions for polymorphism; utility types for transformations. `zod` (or another runtime validator) at the boundary between the type-checked world and untrusted input.
---

# TypeScript (production)

## tsconfig baseline

    {
      "compilerOptions": {
        "target": "ES2022",
        "module": "ESNext",
        "moduleResolution": "Bundler",
        "strict": true,
        "noUncheckedIndexedAccess": true,
        "noImplicitOverride": true,
        "exactOptionalPropertyTypes": true,
        "isolatedModules": true,
        "jsx": "preserve",
        "skipLibCheck": true,
        "esModuleInterop": true,
        "resolveJsonModule": true
      }
    }

* `strict: true` turns on all the strict flags.
* `noUncheckedIndexedAccess` — `arr[0]` is `T | undefined`, not `T`. Prevents an entire class
  of bugs.
* `skipLibCheck: true` — don't type-check `node_modules/*.d.ts`. Faster; almost always safe.

## Types vs interfaces

* `type` for unions, primitives, mapped/computed types. Cannot be extended after declaration.
* `interface` for object shapes that might be extended by consumers or declaration-merged.
* Convention: use `type` unless you need `interface` semantics. Both compile identically.

## Never `any`

    // BAD
    function process(data: any) { return data.value.trim(); }

    // GOOD
    function process(data: unknown) {
      if (typeof data === "object" && data !== null && "value" in data
          && typeof (data as { value: unknown }).value === "string") {
        return (data as { value: string }).value.trim();
      }
      throw new Error("bad shape");
    }

    // BEST (validator)
    const schema = z.object({ value: z.string() });
    function process(data: unknown) {
      return schema.parse(data).value.trim();
    }

* `any` disables type-checking. `unknown` requires narrowing.
* At the network boundary: validate with `zod` / `valibot` / `arktype`. Never trust the wire.

## Generics

    function first<T>(items: T[]): T | undefined {
      return items[0];
    }

    type Repo<T> = {
      get(id: string): Promise<T | null>;
      list(): Promise<T[]>;
    };

* Generic parameter names: `T`, `U`, `V` for utility; DESCRIPTIVE (`TUser`, `TRow`) when
  meaningful in complex signatures.
* Constrain: `<T extends { id: string }>` — the caller must provide something with `.id`.

## Discriminated unions

    type Response =
      | { status: "ok"; data: User }
      | { status: "error"; error: string };

    function handle(r: Response) {
      if (r.status === "ok") { return r.data; }   // narrowed to { status: "ok"; data: User }
      throw new Error(r.error);                    // narrowed to error branch
    }

* Encode "one-of" with a discriminator field. TS narrows perfectly.
* Better than optional properties (`data?: User; error?: string`) which don't express the
  exclusion.

## Utility types

* `Partial<T>` — all properties optional.
* `Required<T>` — all properties required.
* `Pick<T, K>` — subset of keys.
* `Omit<T, K>` — everything except K.
* `Readonly<T>` — no mutation.
* `Record<K, V>` — `{ [key: K]: V }` typed.
* `ReturnType<F>`, `Parameters<F>`, `Awaited<P>` — extract from function / promise types.

## React with TypeScript

    interface UserCardProps { user: User; onClick?: (id: string) => void; }
    export function UserCard({ user, onClick }: UserCardProps) {
      return <button onClick={() => onClick?.(user.id)}>{user.name}</button>;
    }

* Never type props as `any` or `React.FC` (deprecated pattern).
* Event handlers get the right event type: `React.MouseEvent<HTMLButtonElement>`.
* `useState<User | null>(null)` — explicit generic when the inferred type is too narrow.

## Async

    async function fetchUser(id: string): Promise<User> { … }

    // Awaited
    type Fetched = Awaited<ReturnType<typeof fetchUser>>;   // User

* Return `Promise<T>` explicitly on public functions.
* `Awaited<>` unwraps promise types.

## Errors

    // Result type — often cleaner than throw/catch for expected failures
    type Result<T, E = string> = { ok: true; value: T } | { ok: false; error: E };

    async function fetchUser(id: string): Promise<Result<User>> {
      const r = await fetch(`/users/${id}`);
      if (!r.ok) return { ok: false, error: `HTTP ${r.status}` };
      return { ok: true, value: await r.json() };
    }

* Throw for TRULY exceptional cases (invariant broken, programmer error).
* Return `Result<>` for expected failures (network, validation, not found). The caller must
  handle both branches — the compiler enforces it.

## Anti-patterns

* `any` — see above.
* `as` type assertion when TS is right and you're wrong — usually means the types are actually
  incompatible.
* `!` non-null assertion — nearly always a bug waiting. Prefer narrowing.
* `object` type — use `Record<string, unknown>` or a specific interface.
* `Function` type — use `(args: X) => Y`.
* `enum` — prefer const object + type union (`const Status = { ok: "ok", err: "err" } as const;
  type Status = typeof Status[keyof typeof Status];`). Numeric enums have runtime cost + weird
  reverse mapping.
* Types defined in the same file as they're used only, when many files need them — extract.
