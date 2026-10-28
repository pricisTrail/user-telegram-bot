from asyncio import sleep, Lock
from secrets import token_urlsafe
from telegraph.aio import Telegraph
from telegraph.exceptions import RetryAfterError

from ... import LOGGER


class TelegraphHelper:
    def __init__(self, author_name=None, author_url=None):
        self._telegraph = Telegraph(domain="graph.org")
        self._author_name = author_name
        self._author_url = author_url
        self._account_ready = False
        self._account_lock = Lock()

    async def create_account(self):
        async with self._account_lock:
            if self._account_ready:
                return True
            LOGGER.info("Creating Telegraph Account")
            try:
                await self._telegraph.create_account(
                    short_name=token_urlsafe(8),
                    author_name=self._author_name,
                    author_url=self._author_url,
                )
            except Exception as exc:
                LOGGER.error("Unable to create Telegraph account (%s)", type(exc).__name__)
                return False
            self._account_ready = True
            return True

    async def create_page(self, title, content):
        if not await self.create_account():
            raise RuntimeError("Unable to prepare the search results page. Please retry later.")
        try:
            return await self._telegraph.create_page(
                title=title,
                author_name=self._author_name,
                author_url=self._author_url,
                html_content=content,
            )
        except RetryAfterError as st:
            LOGGER.warning(
                f"Telegraph Flood control exceeded. I will sleep for {st.retry_after} seconds."
            )
            await sleep(st.retry_after)
            return await self.create_page(title, content)

    async def edit_page(self, path, title, content):
        if not await self.create_account():
            raise RuntimeError("Unable to prepare the search results page. Please retry later.")
        try:
            return await self._telegraph.edit_page(
                path=path,
                title=title,
                author_name=self._author_name,
                author_url=self._author_url,
                html_content=content,
            )
        except RetryAfterError as st:
            LOGGER.warning(
                f"Telegraph Flood control exceeded. I will sleep for {st.retry_after} seconds."
            )
            await sleep(st.retry_after)
            return await self.edit_page(path, title, content)

    async def edit_telegraph(self, path, telegraph_content):
        nxt_page = 1
        prev_page = 0
        num_of_path = len(path)
        for content in telegraph_content:
            if nxt_page == 1:
                content += (
                    f'<b><a href="https://telegra.ph/{path[nxt_page]}">Next</a></b>'
                )
                nxt_page += 1
            else:
                if prev_page <= num_of_path:
                    content += f'<b><a href="https://telegra.ph/{path[prev_page]}">Prev</a></b>'
                    prev_page += 1
                if nxt_page < num_of_path:
                    content += f'<b> | <a href="https://telegra.ph/{path[nxt_page]}">Next</a></b>'
                    nxt_page += 1
            await self.edit_page(
                path=path[prev_page],
                title="Mirror-leech-bot Torrent Search",
                content=content,
            )
        return


telegraph = TelegraphHelper(
    "Mirror-Leech-Telegram-Bot", "https://github.com/anasty17/mirror-leech-telegram-bot"
)
