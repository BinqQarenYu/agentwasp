## 2024-05-17 - [Vector Similarity Speedup]
**Learning:** In pure Python semantic search implementations without NumPy/pgvector, using `math.hypot(*vec)` for L2-norm is ~2x faster than a generator expression, and `sum(map(operator.mul, a, b))` is ~1.5x faster than `sum(x * y for x, y in zip(a, b))` for cosine similarity. Since vector search loops over many vectors, these constant factors add up measurably.
**Action:** Use these built-in C-implemented functions for vector arithmetic in pure Python where external libraries aren't used.
## 2026-05-17 - Asynchronous Selenium and Thread Usage
 **Learning:** In an async context, treating synchronous Selenium methods as properties (like `driver.title` or `driver.current_url`) will trigger synchronous HTTP calls and block the event loop if unwrapped.
 **Action:** Instead of `time.sleep()`, use `await asyncio.sleep()`. Convert functions with blocking driver interactions to `async def` and wrap properties with `await asyncio.to_thread(getattr, driver, "title")`.

## 2026-05-19 - Eliminate fixed sleeps in smart navigation
 **Learning:** In browser automation tasks like smart navigation, fixed hardcoded sleeps (e.g., `time.sleep(0.3)` in a loop, or sequential `await asyncio.sleep(0.8)`) block the thread unnecessarily and stack up dramatically per page load, causing multi-second unneeded delays.
 **Action:** Prefer dynamic waiting strategies using `WebDriverWait` combined with DOM condition checks (like `document.body.innerText.length` or `document.readyState`) which return immediately once content is ready, and eliminate hardcoded static wait buffering where conditions guarantee readiness.
