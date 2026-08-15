from __future__ import annotations

import argparse
import logging
import sys

from .bark import BarkNotifier
from .config import Settings, ensure_alert_link_secret, load_dotenv
from .monitor import AlertEngine, AlertState
from .oauth import oauth_login
from .provider import LongbridgeProvider
from .service import MonitorService
from .web import run_server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Longbridge 持仓涨跌监控与 Bark 告警")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="检查一次、打印结果并退出")
    mode.add_argument("--list", action="store_true", help="仅列出当前持仓并退出")
    mode.add_argument("--test-bark", action="store_true", help="发送一条 Bark 测试通知并退出")
    mode.add_argument("--web", action="store_true", help="启动 K 线 HTTP 服务")
    mode.add_argument("--oauth-login", action="store_true", help="完成一次 Python SDK OAuth 授权")
    parser.add_argument("--env-file", default=".env", help="环境变量文件，默认 .env")
    parser.add_argument("--host", help="HTTP 监听地址，默认读取 HTTP_HOST")
    parser.add_argument("--port", type=int, help="HTTP 监听端口，默认读取 HTTP_PORT")
    return parser


def _print_snapshots(snapshots) -> None:
    if not snapshots:
        print("当前没有证券持仓。")
        return
    print(f"{'证券':<22} {'现价':>12} {'当日':>10} {'持仓盈亏':>12} {'数量':>12}")
    for snapshot in snapshots:
        day = snapshot.quote.day_change_pct
        cost = snapshot.cost_change_pct
        day_text = "--" if day is None else f"{day:+.2f}%"
        cost_text = "--" if cost is None else f"{cost:+.2f}%"
        label = f"{snapshot.position.name}({snapshot.position.symbol})"
        print(
            f"{label:<22} {str(snapshot.quote.last_price):>12} "
            f"{day_text:>10} {cost_text:>12} {str(snapshot.position.quantity):>12}"
        )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    load_dotenv(args.env_file)
    ensure_alert_link_secret(args.env_file)
    try:
        settings = Settings.from_env()
        logging.basicConfig(
            level=getattr(logging, settings.log_level, logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        notifier = BarkNotifier(
            server_url=settings.bark_server_url,
            device_key=settings.bark_device_key,
            group=settings.bark_group,
            level=settings.bark_level,
            sound=settings.bark_sound,
        )
        if args.oauth_login:
            settings.validate_oauth()
            oauth_login(settings.oauth_client_id, settings.oauth_callback_port)
            return 0
        if args.web:
            def web_provider() -> LongbridgeProvider:
                # The dashboard can start before credentials exist. Reloading here means
                # adding .env later activates the next request without a server restart.
                load_dotenv(args.env_file)
                return LongbridgeProvider(
                    include_extended_hours=settings.include_extended_hours,
                    batch_size=settings.quote_batch_size,
                    oauth_client_id=settings.oauth_client_id,
                    oauth_callback_port=settings.oauth_callback_port,
                )

            run_server(
                host=args.host or settings.http_host,
                port=args.port or settings.http_port,
                provider_factory=web_provider,
                env_path=args.env_file,
            )
            return 0
        if args.test_bark:
            if not settings.bark_device_key:
                raise ValueError("缺少 BARK_DEVICE_KEY")
            notifier.send("LongWatch 测试成功", "Bark 告警通道已配置完成。")
            print("Bark 测试通知已发送。")
            return 0

        settings.validate(require_bark=not args.list)
        provider = LongbridgeProvider(
            include_extended_hours=settings.include_extended_hours,
            batch_size=settings.quote_batch_size,
            oauth_client_id=settings.oauth_client_id,
            oauth_callback_port=settings.oauth_callback_port,
        )
        engine = AlertEngine(
            AlertState(settings.state_file),
            day_rise_pct=settings.day_rise_pct,
            day_fall_pct=settings.day_fall_pct,
            execution_rise_pct=settings.execution_rise_pct,
            execution_fall_pct=settings.execution_fall_pct,
            hysteresis_pct=settings.alert_hysteresis_pct,
            detail_base_url=settings.alert_detail_base_url,
            detail_secret=settings.alert_link_secret,
        )
        service = MonitorService(
            provider=provider,
            notifier=notifier,
            engine=engine,
            poll_interval_seconds=settings.poll_interval_seconds,
            positions_refresh_seconds=settings.positions_refresh_seconds,
        )
        if args.list:
            service.refresh_positions(force=True)
            for position in service.positions:
                print(
                    f"{position.symbol}\t{position.name}\t数量={position.quantity}\t"
                    f"成本={position.cost_price} {position.currency}"
                )
            if not service.positions:
                print("当前没有证券持仓。")
            return 0
        if args.once:
            _print_snapshots(service.check_once(force_positions_refresh=True))
            return 0
        service.run_forever()
        return 0
    except (ValueError, RuntimeError) as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
