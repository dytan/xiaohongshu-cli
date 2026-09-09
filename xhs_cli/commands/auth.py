"""Authentication commands: login, status, logout."""

import time

import click

from ..client import XhsClient
from ..command_normalizers import normalize_xhs_user_payload
from ..cookies import clear_cookies, get_cookie_path, get_cookies, parse_cookie_string, save_cookies
from ..exceptions import XhsApiError
from ..formatter import (
    console,
    maybe_print_structured,
    print_success,
    render_user_info,
    success_payload,
)
from ._common import exit_for_error, handle_errors, run_client_action, structured_output_options


def _emit_payload(data: dict[str, object], *, as_json: bool, as_yaml: bool) -> bool:
    """Emit a structured success payload when requested."""
    return maybe_print_structured(success_payload(data), as_json=as_json, as_yaml=as_yaml)


def _is_valid_login(user: dict[str, object]) -> bool:
    """Check whether the normalized user payload represents a real logged-in session."""
    if user.get("guest"):
        return False
    nickname = user.get("nickname", "")
    return bool(nickname and nickname != "Unknown")


def _print_login_success(user: dict[str, object]) -> None:
    """Print a concise login success message."""
    print_success(f"Logged in as: {user['nickname']} (ID: {user['red_id']})")


def _print_status_summary(user: dict[str, object]) -> None:
    """Render a short authenticated-user summary."""
    console.print("[bold green]✓ Logged in[/bold green]")
    console.print(f"  昵称: [bold]{user['nickname']}[/bold]")
    if user["red_id"]:
        console.print(f"  小红书号: {user['red_id']}")
    if user["ip_location"]:
        console.print(f"  IP 属地: {user['ip_location']}")
    if user["desc"]:
        console.print(f"  简介: {user['desc']}")


@click.command()
@click.option(
    "--cookies",
    envvar="XHS_COOKIES",
    help="Browser Cookie header string (env: XHS_COOKIES; command-line values may enter shell history)",
)
def auth(cookies: str | None):
    """Import a browser Cookie header string and save it for later commands."""
    if cookies is None:
        console.print("Open https://www.xiaohongshu.com/login in your browser and log in.")
        cookies = click.prompt("Paste the Cookie request header", hide_input=True)
    try:
        parsed = parse_cookie_string(cookies)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from None
    save_cookies(parsed)
    print_success(f"Saved {len(parsed)} cookies to {get_cookie_path()}")


@click.command()
@click.option(
    "--cookie-source",
    type=str,
    default=None,
    help="Browser to read cookies from (default: auto-detect all installed browsers)",
)
@structured_output_options
@click.option(
    "--qrcode-status",
    is_flag=True,
    help="Check a detached QR login started with --qrcode --async",
)
@click.option(
    "--async",
    "async_qrcode",
    is_flag=True,
    help="Print the QR code and continue login in a detached worker",
)
@click.option(
    "--qrcode",
    "use_qrcode",
    is_flag=True,
    default=False,
    help="Login via QR code (scan with Xiaohongshu app)",
)
@click.pass_context
def login(
    ctx,
    cookie_source: str | None,
    as_json: bool,
    as_yaml: bool,
    qrcode_status: bool,
    async_qrcode: bool,
    use_qrcode: bool,
):
    """Log in by extracting cookies from browser, or via QR code."""

    if qrcode_status:
        if use_qrcode or async_qrcode:
            raise click.UsageError("--qrcode-status cannot be combined with --qrcode or --async")
        from ..qr_login_job import get_qr_login_status

        state = get_qr_login_status()
        if not _emit_payload(state, as_json=as_json, as_yaml=as_yaml):
            status_name = str(state.get("status", "not_started"))
            message = str(state.get("message", "")).strip()
            console.print(f"QR login status: [bold]{status_name}[/bold]")
            if message:
                console.print(message)
            qr_url = str(state.get("qr_url", "")).strip()
            if qr_url:
                console.print(f"QR URL: {qr_url}")
            if status_name == "succeeded":
                console.print("Run `xhs status` to verify the saved session.")
        return

    if async_qrcode and not use_qrcode:
        raise click.UsageError("--async requires --qrcode")

    if use_qrcode:
        if async_qrcode:
            from ..qr_login import _display_login_qr
            from ..qr_login_job import start_qr_login

            try:
                state = start_qr_login()
            except RuntimeError as exc:
                exit_for_error(
                    XhsApiError(str(exc)),
                    as_json=as_json,
                    as_yaml=as_yaml,
                    prefix="QR login failed",
                )
            if state.get("status") == "failed":
                exit_for_error(
                    XhsApiError(str(state.get("message", "QR login worker failed"))),
                    as_json=as_json,
                    as_yaml=as_yaml,
                    prefix="QR login failed",
                )
            qr_url = str(state.get("qr_url", ""))
            if not qr_url:
                exit_for_error(
                    XhsApiError("QR login worker did not return a QR URL"),
                    as_json=as_json,
                    as_yaml=as_yaml,
                    prefix="QR login failed",
                )
            from ..qr_login import render_qr_variants

            qr_payload = {**state, **render_qr_variants(qr_url)}
            if not _emit_payload(qr_payload, as_json=as_json, as_yaml=as_yaml):
                _display_login_qr(qr_url, None)
                console.print(
                    "\nThe login worker is running in the background. "
                    "After confirming in the app, run `xhs login --qrcode-status`, "
                    "then `xhs status`."
                )
            return

        def _login_with_qrcode() -> None:
            from ..qr_login import qrcode_login

            cookies = qrcode_login(prefer_browser_assisted=True)

            # Verify by fetching user info (may return guest=true briefly)
            import time
            time.sleep(1)  # brief delay for session propagation
            with XhsClient(cookies) as client:
                info = client.get_self_info()
            user = normalize_xhs_user_payload(info)

            if user["guest"]:
                # Session not yet propagated; still valid
                if not _emit_payload(
                    {"authenticated": True, "user": {"id": user["id"]}},
                    as_json=as_json,
                    as_yaml=as_yaml,
                ):
                    print_success("Logged in (session saved)")
            else:
                if not _emit_payload({"authenticated": True, "user": user}, as_json=as_json, as_yaml=as_yaml):
                    _print_login_success(user)

        handle_errors(
            _login_with_qrcode,
            as_json=as_json,
            as_yaml=as_yaml,
            prefix="QR login failed",
        )
        return

    # Browser cookie extraction (default)
    if cookie_source is None:
        cookie_source = ctx.obj.get("cookie_source", "auto") if ctx.obj else "auto"

    def _login_with_browser() -> None:
        browser, cookies = get_cookies(cookie_source, force_refresh=True)
        print_success(f"Cookies extracted from {browser}")

        # Verify by fetching user info, retry once if session not yet propagated
        with XhsClient(cookies) as client:
            info = client.get_self_info()
        user = normalize_xhs_user_payload(info)

        if not _is_valid_login(user):
            time.sleep(2.5)
            with XhsClient(cookies) as client:
                info = client.get_self_info()
            user = normalize_xhs_user_payload(info)

        if not _is_valid_login(user):
            raise XhsApiError(
                "Browser cookies were extracted, but the session appears invalid "
                "(guest or incomplete profile). Try: xhs login --qrcode"
            )

        if not _emit_payload({"authenticated": True, "user": user}, as_json=as_json, as_yaml=as_yaml):
            _print_login_success(user)

    handle_errors(
        _login_with_browser,
        as_json=as_json,
        as_yaml=as_yaml,
        prefix="Login verification failed",
    )


@click.command()
@structured_output_options
@click.pass_context
def status(ctx, as_json: bool, as_yaml: bool):
    """Check current login status and user info."""
    def _show_status() -> None:
        info = run_client_action(ctx, lambda client: client.get_self_info())
        user = normalize_xhs_user_payload(info)

        if not _emit_payload({"authenticated": True, "user": user}, as_json=as_json, as_yaml=as_yaml):
            _print_status_summary(user)

    handle_errors(_show_status, as_json=as_json, as_yaml=as_yaml, prefix="Status check failed")


@click.command()
@structured_output_options
@click.pass_context
def logout(ctx, as_json: bool, as_yaml: bool):
    """Clear saved cookies and log out."""
    clear_cookies()
    if not _emit_payload({"logged_out": True}, as_json=as_json, as_yaml=as_yaml):
        print_success("Logged out — cookies cleared")


@click.command()
@structured_output_options
@click.pass_context
def whoami(ctx, as_json: bool, as_yaml: bool):
    """Show detailed profile of current user (level, fans, likes)."""
    def _show_profile() -> None:
        info = run_client_action(ctx, lambda client: client.get_self_info())
        user = normalize_xhs_user_payload(info)

        if not _emit_payload({"user": user}, as_json=as_json, as_yaml=as_yaml):
            render_user_info(info)

    handle_errors(_show_profile, as_json=as_json, as_yaml=as_yaml, prefix="Failed to get profile")
