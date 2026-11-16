import asyncio
import os
from typing import Any, Awaitable, Callable, List, Optional

from playwright.async_api import BrowserContext, Page
from playwright_stealth import Stealth


WARM_POOL_SIZE: int = int(os.getenv("WARM_POOL_SIZE", "1"))


class WarmTabPool:
    """Maintains pre-warmed Gemini browser tabs so new/reset sessions start with near-zero delay."""

    def __init__(self, pool_size: int = WARM_POOL_SIZE) -> None:
        self.pool_size = max(0, pool_size)
        self._pool: List[Page] = []
        self._lock = asyncio.Lock()
        self._refill_task: Optional[asyncio.Task] = None

    async def _is_page_usable(
        self,
        page: Page,
        wait_for_editor_ready: Callable[[Page, int], Awaitable[bool]],
    ) -> bool:
        try:
            if page.is_closed():
                return False
            url = (page.url or "").lower()
            if any(k in url for k in ("signin", "servicelogin", "accounts.google.com")):
                return False
            return await wait_for_editor_ready(page, 2000)
        except Exception:
            return False

    async def acquire(
        self,
        context: Optional[BrowserContext],
        target_url: str,
        wait_for_editor_ready: Callable[[Page, int], Awaitable[bool]],
        active_tab_count_fn: Callable[[], int],
        max_open_tabs: int,
    ) -> Optional[Page]:
        if self.pool_size <= 0:
            return None

        acquired: Optional[Page] = None
        async with self._lock:
            while self._pool:
                candidate = self._pool.pop(0)
                if await self._is_page_usable(candidate, wait_for_editor_ready):
                    acquired = candidate
                    break
                try:
                    if not candidate.is_closed():
                        await candidate.close()
                except Exception:
                    pass

        if context is not None:
            self.schedule_refill(
                context=context,
                target_url=target_url,
                wait_for_editor_ready=wait_for_editor_ready,
                active_tab_count_fn=active_tab_count_fn,
                max_open_tabs=max_open_tabs,
            )

        if acquired is not None:
            print("⚡ [TabPool] Acquired pre-warmed tab (<0.1s)", flush=True)
        return acquired

    def schedule_refill(
        self,
        context: Optional[BrowserContext],
        target_url: str,
        wait_for_editor_ready: Callable[[Page, int], Awaitable[bool]],
        active_tab_count_fn: Callable[[], int],
        max_open_tabs: int,
    ) -> None:
        if self.pool_size <= 0 or context is None:
            return
        if self._refill_task and not self._refill_task.done():
            return
        self._refill_task = asyncio.create_task(
            self.ensure_warm(
                context=context,
                target_url=target_url,
                wait_for_editor_ready=wait_for_editor_ready,
                active_tab_count_fn=active_tab_count_fn,
                max_open_tabs=max_open_tabs,
            )
        )

    async def ensure_warm(
        self,
        context: Optional[BrowserContext],
        target_url: str,
        wait_for_editor_ready: Callable[[Page, int], Awaitable[bool]],
        active_tab_count_fn: Callable[[], int],
        max_open_tabs: int,
    ) -> None:
        if self.pool_size <= 0 or context is None:
            return

        async with self._lock:
            # Prune any closed tabs first
            alive: List[Page] = []
            for p in self._pool:
                if not p.is_closed():
                    alive.append(p)
            self._pool = alive

            while len(self._pool) < self.pool_size:
                if (active_tab_count_fn() + len(self._pool)) >= max_open_tabs:
                    break
                new_page: Optional[Page] = None
                try:
                    new_page = await context.new_page()
                    await Stealth().apply_stealth_async(new_page)
                    await new_page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
                    if await wait_for_editor_ready(new_page, 30000):
                        self._pool.append(new_page)
                        print(f"🔥 [TabPool] Pre-warmed tab ready ({len(self._pool)}/{self.pool_size})", flush=True)
                    else:
                        await new_page.close()
                        break
                except asyncio.CancelledError:
                    if new_page and not new_page.is_closed():
                        try:
                            await new_page.close()
                        except Exception:
                            pass
                    raise
                except Exception as exc:
                    print(f"⚠️ [TabPool] Pre-warm skipped: {exc}", flush=True)
                    if new_page and not new_page.is_closed():
                        try:
                            await new_page.close()
                        except Exception:
                            pass
                    break

    async def close_all(self) -> None:
        if self._refill_task and not self._refill_task.done():
            self._refill_task.cancel()
            try:
                await self._refill_task
            except asyncio.CancelledError:
                pass
        self._refill_task = None
        async with self._lock:
            for p in self._pool:
                try:
                    if not p.is_closed():
                        await p.close()
                except Exception:
                    pass
            self._pool.clear()

    def get_status(self) -> dict[str, Any]:
        alive_count = sum(1 for p in self._pool if not p.is_closed())
        return {
            "enabled": self.pool_size > 0,
            "target_size": self.pool_size,
            "warm_tabs": alive_count,
        }


warm_tab_pool = WarmTabPool()
