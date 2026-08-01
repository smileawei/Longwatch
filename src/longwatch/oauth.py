from __future__ import annotations

import webbrowser


def oauth_login(client_id: str, callback_port: int = 60355) -> None:
    """Run the one-time SDK OAuth flow and persist its refresh token."""
    try:
        from longbridge.openapi import Config, OAuthBuilder, QuoteContext
    except ImportError as exc:
        raise RuntimeError("未安装 longbridge SDK，请先执行 pip install -e .") from exc

    def open_authorization(url: str) -> None:
        print("请在浏览器完成 Longbridge 授权：", flush=True)
        print(url, flush=True)
        if not webbrowser.open(url):
            print("浏览器未自动打开，请复制上面的链接。", flush=True)

    oauth = OAuthBuilder(client_id, callback_port).build(open_authorization)
    config = Config.from_oauth(oauth)
    # A real request verifies that the OAuth handle can create an API session.
    quotes = QuoteContext(config).quote(["NVDA.US"])
    if not quotes:
        raise RuntimeError("OAuth 已保存，但 NVDA.US 行情验证没有返回数据")
    quote = quotes[0]
    print(f"OAuth 登录成功：NVDA.US {quote.last_done}", flush=True)
