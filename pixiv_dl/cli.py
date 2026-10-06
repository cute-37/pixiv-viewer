import argparse
import sys
import threading

from pixiv_dl.applog import setup_logging
from pixiv_dl.config import Config, VERSION, mask_token


class CliProgress:
    """用 tqdm 显示 Processor.job 的进度（后台线程轮询 JobState）。"""

    def __init__(self, pro):
        self.pro = pro
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=3)
        return False

    def _run(self):
        from tqdm import tqdm
        bar, cur = None, None
        while True:
            j = self.pro.job
            if j.kind != "idle" and j.id != cur:
                if bar:
                    bar.close()
                bar = tqdm(total=j.total or None, desc=j.kind, unit="项", dynamic_ncols=True)
                cur = j.id
            if bar:
                bar.set_description(f"{j.kind}/{j.phase}" if j.phase else j.kind)
                bar.total = j.total or None
                bar.n = j.done
                post = {"成功": j.success, "失败": j.failed}
                if j.bytes:
                    post["数据"] = self.pro._format_size(j.bytes)
                if j.message:
                    post["提示"] = j.message
                bar.set_postfix(post, refresh=False)
                bar.refresh()
            if self._stop.is_set():
                break
            self._stop.wait(0.5)
        if bar:
            bar.close()


def print_header():
    print(f"\n{'=' * 60}\n              Pixiv 下载管理器 v{VERSION}\n{'=' * 60}")
    save_path = Config.storage_target()
    print(f" [模式] {Config.STORAGE_MODE.upper()}    [保存] {save_path}\n{'-' * 60}")
    accounts = Config.get_accounts()
    if not accounts:
        print(" 账号状态: 未配置任何账号（按 A 添加）")
    else:
        for name, info in accounts.items():
            mark = "主" if name == Config.MAIN_ACCOUNT else "备"
            print(f"  [{mark}] {name} ({info.get('username') or '未知'}) - "
                  f"{'有效' if info.get('is_valid', True) else '无效'}"
                  f"{' [' + info['remark'] + ']' if info.get('remark') else ''}")
    print('-' * 60)


def suggest_next(pro):
    """菜单顶部的「下一步建议」，和前端总览页的提示一致。"""
    from datetime import datetime
    if not Config.get_accounts():
        return "还没有账号：按 A 添加账号"
    try:
        p = pro.db.plan_numbers(Config.MAX_ATTEMPTS)
        failed = pro.db.stats()['failed']
    except Exception:
        return None
    hints = []
    if not p['last_sync']:
        hints.append("还没有同步过：按 U 开始第一次「检查更新并下载」")
    else:
        try:
            age = datetime.now() - datetime.strptime(str(p['last_sync'])[:19], "%Y-%m-%d %H:%M:%S")
            when = f"{age.days} 天前" if age.days else f"{age.seconds // 3600} 小时前"
        except ValueError:
            when, age = "未知", None
        if p['pending']:
            hints.append(f"有 {p['pending']} 个文件待下载（上次同步 {when}）：按 U 一键更新，或按 4 仅下载")
        elif age is not None and age.days >= 1:
            hints.append(f"距离上次同步已 {when}：按 U 检查更新")
        else:
            hints.append(f"一切就绪（上次同步 {when}）：按 U 检查更新")
    if failed:
        hints.append(f"有 {failed} 个失败文件：在 Web 前端「任务」页按原因处理，或 8 → 4 全部重试")
    return "；".join(hints)


def print_job_summary(pro):
    """任务结束后打印一段人话总结（对应前端的结果卡片）。"""
    j = pro.job
    if j.kind == "idle":
        return
    snap = j.snapshot(with_logs=False)
    r, d = snap['result'], snap['detail']
    label = {'done': '完成', 'cancelled': '已取消', 'error': '出错'}.get(j.status, j.status)
    print(f"\n── 任务{label} ──")
    if j.status == 'error':
        print(f"  错误: {j.error}")
    if 'artists' in r:
        print(f"  同步: 扫描 {r['artists']} 位画师，新增 {r.get('new_works', 0)} 个作品（{r.get('new_files', 0)} 个文件），"
              f"失败 {r.get('artists_failed', 0)} 位")
        for e in sorted(d.get('new', {}).values(), key=lambda x: -x.get('works', 0))[:8]:
            print(f"    + {e.get('name')}: {e.get('works')} 个作品")
        for e in list(d.get('failed', {}).values())[:5]:
            print(f"    ! {e.get('name')}: {e.get('note')}")
    if 'tasks' in r:
        print(f"  下载: 成功 {r.get('success', 0) - r.get('skipped', 0)} 个（已存在跳过 {r.get('skipped', 0)}），"
              f"失败 {r.get('failed', 0)} 个，{pro._format_size(r.get('bytes', 0))}")
        names = {'deleted': '作品已删除', 'restricted': '无权查看', 'network': '网络/服务器', 'http': '被拒绝(403)', 'storage': '存储写入',
                 'content': '内容异常', 'api': 'API 错误', 'auth': '账号失效', 'other': '其他'}
        kinds = d.get('fail_kinds', {})
        if kinds:
            print("  失败原因: " + "，".join(f"{names.get(k, k)} {v.get('count', 0)}" for k, v in kinds.items()))


def ask_int(prompt, lo, hi, current=None):
    raw = input(prompt).strip()
    if raw.isdigit() and lo <= int(raw) <= hi:
        return int(raw)
    if raw:
        print(f"请输入 {lo}~{hi} 之间的整数")
    return current


def menu_account_manage():
    from pixiv_dl.token_manager import get_new_token_flow, refresh_token_flow, test_all_tokens
    while True:
        print("\n=== 账号管理 ===\n 1. 添加新账号 (获取新Token)\n 2. 刷新现有账号的Token\n 3. 查看所有账号信息"
              "\n 4. 测试所有Token可用性\n 5. 设置主要账号\n 6. 删除账号\n 0. 返回上级")
        choice = input("请选择: ").strip()
        names = list(Config.TOKENS.keys())
        if choice == '1':
            get_new_token_flow()
        elif choice == '2':
            refresh_token_flow()
        elif choice == '3':
            if not names:
                print("暂无保存的账号")
            for name, info in Config.TOKENS.items():
                print(f"  • {name}{' [主要]' if name == Config.MAIN_ACCOUNT else ''}: {info.get('username') or '未知'} "
                      f"(ID:{info.get('user_id') or '-'}) token={mask_token(info['token'])} "
                      f"{'有效' if info.get('is_valid', True) else '无效'} 最后测试:{info.get('last_tested') or '从未'}")
        elif choice == '4':
            test_all_tokens()
        elif choice in ('5', '6'):
            if not names:
                print("暂无保存的账号")
                continue
            for i, n in enumerate(names, 1):
                print(f"  {i}. {n}{' [主要]' if n == Config.MAIN_ACCOUNT else ''}")
            idx = ask_int("请选择序号 (0=取消): ", 1, len(names))
            if idx:
                name = names[idx - 1]
                if choice == '5':
                    Config.set_main_account(name)
                    print(f"已设置 '{name}' 为主要账号")
                elif input(f"确认删除账号 '{name}'? (y/n): ").strip().lower() == 'y':
                    Config.remove_token(name)
                    print(f"已删除账号 '{name}'")
        elif choice == '0':
            return
        else:
            print("无效选择，请重试。")


def menu_settings(pro):
    while True:
        print("\n=== 系统设置 ===\n 1. 查看当前配置\n 2. 调整线程数设置")
        print(f" 3. 调整增量扫描回看数 [当前: {Config.METADATA_REFRESH_LIMIT}]")
        print(f" 4. 调整失败判定阈值 [当前: {Config.FAILURE_RATE_THRESHOLD}]")
        print(f" 5. 切换风控保护 [当前: {'开' if Config.RATE_LIMIT_ENABLED else '关'}]")
        print(f" 6. 切换存储模式 [当前: {Config.STORAGE_MODE.upper()}]\n 7. 修改存储路径/连接信息")
        print(" 8. 保存为预设\n 9. 加载预设\n 0. 返回上级")
        c = input("请选择: ").strip()
        if c == '1':
            for k, v in Config.public_view().items():
                print(f" {k} = {v}")
            input("\n按回车键返回...")
        elif c == '2':
            fields = [('MAIN_ACCOUNT_SYNC_THREADS', '主账号同步线程数'), ('BACKUP_ACCOUNT_SYNC_THREADS', '备用账号同步线程数'),
                      ('MAIN_ACCOUNT_DOWNLOAD_THREADS', '主账号下载线程数'), ('BACKUP_ACCOUNT_DOWNLOAD_THREADS', '备用账号下载线程数')]
            for i, (k, label) in enumerate(fields, 1):
                print(f" {i}. {label} [当前: {getattr(Config, k)}]")
            idx = ask_int("选择要修改的项 (0=返回): ", 1, len(fields))
            if idx:
                k = fields[idx - 1][0]
                v = ask_int("新值 (1-8): ", 1, 8)
                if v:
                    setattr(Config, k, v)
                    Config.save_settings()
                    print(">>> 已更新并保存")
        elif c == '3':
            v = ask_int("新回看数量 (0=不回看): ", 0, 1000)
            if v is not None:
                Config.METADATA_REFRESH_LIMIT = v
                Config.save_settings()
                print(">>> 已更新并保存")
        elif c == '4':
            try:
                val = float(input("新失败率阈值 (0.1-0.9): "))
                if 0.1 <= val <= 0.9:
                    Config.FAILURE_RATE_THRESHOLD = val
                    Config.save_settings()
                    print(">>> 已更新并保存")
            except ValueError:
                print("无效输入")
        elif c == '5':
            Config.RATE_LIMIT_ENABLED = not Config.RATE_LIMIT_ENABLED
            Config.save_settings()
            print(f"已{'开启' if Config.RATE_LIMIT_ENABLED else '关闭'}风控保护")
        elif c == '6':
            m = input("1. Local (本地)  2. SMB (NAS): ").strip()
            new_mode = {'1': 'local', '2': 'smb'}.get(m)
            if new_mode and new_mode != Config.STORAGE_MODE and input(f"切换为 {new_mode.upper()}? (y/n): ").lower() == 'y':
                Config.STORAGE_MODE = new_mode
                Config.save_settings()
                pro.reset_storage()
                print(">>> 已切换，下次使用时会按新配置连接")
        elif c == '7':
            _edit_storage(pro)
        elif c == '8':
            name = input("预设名称 (例如 home_nas): ").strip()
            if name and Config.save_preset(name):
                print(f"预设 '{name}' 已保存")
        elif c == '9':
            presets = Config.list_presets()
            if not presets:
                print("暂无预设")
                continue
            for i, p in enumerate(presets, 1):
                print(f" {i}. {p}")
            idx = ask_int("选择加载序号: ", 1, len(presets))
            if idx and Config.load_preset(presets[idx - 1]):
                Config.save_settings()
                pro.reset_storage()
                print(f">>> 预设 '{presets[idx - 1]}' 已加载")
        elif c == '0':
            return


def _edit_storage(pro):
    if Config.STORAGE_MODE == 'local':
        print(f"\n当前本地路径: {Config.LOCAL_SAVE_PATH}")
        new_path = input("新路径: ").strip()
        if new_path:
            ok, msg = Config.validate_connection('local', local_path=new_path)
            if ok:
                Config.LOCAL_SAVE_PATH = new_path
                Config.save_settings()
                pro.reset_storage()
            print((">>> " if ok else "!!! ") + msg)
        return
    print("\n=== 配置 SMB 信息（回车保持不变）===")
    info = {
        'ip': input(f"IP [{Config.NAS_IP}]: ").strip() or Config.NAS_IP,
        'share': input(f"Share [{Config.NAS_SHARE}]: ").strip() or Config.NAS_SHARE,
        'user': input(f"User [{Config.NAS_USER}]: ").strip() or Config.NAS_USER,
        'pass': input("Pass: ").strip() or Config.NAS_PASS,
        'base_path': input(f"Base Path [{Config.NAS_BASE_PATH}]: ").strip() or Config.NAS_BASE_PATH,
        'remote_name': input(f"Remote Name [{Config.NAS_REMOTE_NAME}]: ").strip() or Config.NAS_REMOTE_NAME,
    }
    print("正在测试连接...")
    ok, msg = Config.validate_connection('smb', nas_info=info)
    if ok:
        Config.NAS_IP, Config.NAS_SHARE, Config.NAS_USER = info['ip'], info['share'], info['user']
        Config.NAS_PASS, Config.NAS_BASE_PATH, Config.NAS_REMOTE_NAME = info['pass'], info['base_path'], info['remote_name']
        Config.save_settings()
        pro.reset_storage()
    print((">>> " if ok else "!!! ") + msg + ("，配置已保存" if ok else "，未保存"))


def menu_diagnose(pro):
    while True:
        print("\n=== 诊断与修复 ===\n 1. 预览待下载任务\n 2. 导出预览列表 (CSV)\n 3. 导出失败记录 (CSV)"
              "\n 4. 重试失败任务\n 5. 回收卡住任务\n 6. 存储一致性核查 (数据库 vs 实际文件)\n 7. 清理临时缓存文件\n 0. 返回上级")
        c = input("请选择: ").strip()
        if c == '1':
            n = input("预览数量 [20]: ").strip()
            res = pro.preview_pending(limit=int(n) if n.isdigit() else 20)
            for r in res or []:
                print(f"{r['task_key']} 画师{r['author_id']} 作品{r['illust_id']} p{r['page']} {r['media_type']}")
            if not res:
                print("当前无待处理任务")
        elif c == '2':
            path = input("保存文件名 [preview.csv]: ").strip() or 'preview.csv'
            print(f"已导出至 {pro.export_preview_csv(path)}")
        elif c == '3':
            out = input("保存文件名 [回车使用默认]: ").strip()
            print(f"导出完成: {pro.export_failed_tasks(out or None)}")
        elif c == '4':
            print(f"已重置 {pro.retry_failed_tasks()} 个任务")
        elif c == '5':
            res = pro.reclaim_stuck_tasks()
            print(f"已恢复 {res.get('reclaimed', 0)} 个，永久失败 {res.get('permanent_failed', 0)} 个")
        elif c == '6':
            try:
                with CliProgress(pro):
                    stats = pro.verify_storage(apply=False)
                if stats and (stats['missing'] or stats['restored']):
                    print(f"\n预览结果：缺失 {stats['missing']} 个，可找回 {stats['restored']} 个。")
                    if input("应用修正到数据库? (y/N): ").strip().lower() == 'y':
                        with CliProgress(pro):
                            pro.verify_storage(apply=True)
                else:
                    print("\n数据库与存储一致，无需修正。")
            except Exception as e:
                print(f"核查失败: {e}")
        elif c == '7':
            days = input("清理早于多少天的文件 [回车使用配置值]: ").strip()
            print(f"已清理 {pro.clean_temp(int(days) if days.isdigit() else None)} 个文件")
        elif c == '0':
            return


def menu_reports(pro):
    while True:
        print("\n=== 数据报表 ===\n 1. 总体统计概览\n 0. 返回上级")
        c = input("请选择: ").strip()
        if c == '1':
            s = pro.db.stats()
            print(f"\n--- 数据库概览 ---\n 画师总数: {s['artists']}（关注 {s['artists_followed']}，异常/注销 {s['artists_deleted']}）"
                  f"\n 作品数:   {s['works']}\n 任务总数: {s['tasks']}\n 已下载:   {s['done']}（{pro._format_size(s['bytes_done'])}）"
                  f"\n 待下载:   {s['pending'] + s['running']}\n 失败:     {s['failed']}")
        elif c == '0':
            return


def menu_help():
    print("""
=== 使用说明 ===
 [核心概念]
  * 增量同步: 扫描每个画师的新作品，并刷新最近少量旧作的数据后停止。
  * 全量同步: 无论是否有记录，强制重新扫描画师的所有作品。
  * 下载时会通过 API 重新获取最新的下载地址，不使用数据库里保存的旧地址。
 [常见流程]
  日常: 「同步并自动下载」；补充: 「仅执行下载」；怀疑文件丢失: 「诊断与修复 → 存储一致性核查」
 [前端]
  按 W 启动 Web 前端（浏览器里查看进度、浏览作品、管理账号和设置）。""")
    input("\n按回车键返回...")


def menu_artist_manage(pro):
    while True:
        print("\n=== 画师信息管理 ===\n 1. 查看画师资料\n 2. 刷新指定画师资料 (从API获取)\n 3. 批量补充缺失资料"
              "\n 4. 批量刷新所有画师资料\n 5. 下载缺失的画师头像\n 0. 返回上级")
        c = input("请选择: ").strip()
        limit_txt = lambda: (lambda v: int(v) if v.isdigit() else None)(input("限制数量 [回车=无限制]: ").strip())
        if c in ('1', '2'):
            pid = input("画师PID: ").strip()
            if pid.isdigit():
                if c == '2':
                    pro.ensure_clients()
                    print(">>> 资料更新成功！" if pro.refresh_artist_profile(int(pid)) else ">>> 获取资料失败")
                pro.view_artist_profile(int(pid))
            input("\n按回车键返回...")
        elif c == '3':
            with CliProgress(pro):
                pro.refresh_all_artist_profiles(only_missing=True, limit=limit_txt())
        elif c == '4':
            if input("确定要刷新所有画师资料吗？这可能需要较长时间 (y/n): ").strip().lower() == 'y':
                with CliProgress(pro):
                    pro.refresh_all_artist_profiles(only_missing=False, limit=limit_txt())
        elif c == '5':
            with CliProgress(pro):
                pro.download_missing_avatars(limit=limit_txt())
        elif c == '0':
            return


def run_web(port=None):
    from pixiv_dl.web.server import serve
    serve(port=port, open_browser=True)


def main():
    ap = argparse.ArgumentParser(description="Pixiv 下载管理器")
    ap.add_argument("--web", action="store_true", help="直接启动 Web 前端")
    ap.add_argument("--port", type=int, default=None, help="Web 前端端口")
    args = ap.parse_args()

    if args.web:
        run_web(args.port)
        return

    logger = setup_logging()
    from pixiv_dl.processor import Processor

    if not Config.get_accounts():
        print("\n⚠️  检测到未设置 Pixiv Token，请先完成认证设置。\n")
        from pixiv_dl.token_manager import get_new_token_flow
        if not get_new_token_flow():
            print("\n未添加账号。你仍可以进入菜单（按 A 管理账号，按 W 打开前端）。")

    try:
        pro = Processor()
    except Exception as e:
        logger.error(f"启动失败: {e}")
        return

    while True:
        print_header()
        tip = suggest_next(pro)
        if tip:
            print(f" 💡 下一步建议：{tip}\n" + "-" * 60)
        print(" U. 一键更新（增量同步 + 下载，推荐）\n" + "-" * 30)
        print(" 1. 全量同步 (深度扫描关注列表, 获取所有作品)\n 2. 增量同步 (快速扫描新作品)\n 3. 同步并自动下载 (组合操作)"
              "\n 4. 仅执行下载 (下载数据库中的待下载任务)\n 5. 指定画师同步\n 6. 指定画师操作 (同步并下载)\n" + "-" * 30 +
              "\n 7. 画师信息管理\n 8. 诊断与修复\n 9. 数据报表\n 10. 系统设置\n 11. 使用说明\n A. 账号管理\n W. 启动 Web 前端\n 0. 退出程序")
        c = input("请输入序号选择: ").strip().lower()
        try:
            if c == 'u':
                limit = input("下载数量限制 [回车=无限制]: ").strip()
                with CliProgress(pro):
                    pro.sync_and_download(deep=False, limit=int(limit) if limit.isdigit() else None)
            elif c == '1':
                with CliProgress(pro):
                    pro.sync(deep=True)
            elif c == '2':
                with CliProgress(pro):
                    pro.sync(deep=False)
            elif c == '3':
                mode = input("同步模式 [1=全量 | 2=增量快速(默认)]: ").strip()
                limit = input("下载数量限制 [回车=无限制]: ").strip()
                with CliProgress(pro):
                    pro.sync_and_download(deep=(mode == '1'), limit=int(limit) if limit.isdigit() else None)
            elif c == '4':
                limit = input("下载数量限制 [回车=无限制]: ").strip()
                with CliProgress(pro):
                    pro.download(limit=int(limit) if limit.isdigit() else None)
            elif c in ('5', '6'):
                pid = input("画师PID: ").strip()
                if pid.isdigit():
                    with CliProgress(pro):
                        if c == '5':
                            pro.sync(aid=int(pid), deep=True)
                        else:
                            pro.sync_and_download_artist(int(pid))
            elif c == '7':
                menu_artist_manage(pro)
            elif c == '8':
                menu_diagnose(pro)
            elif c == '9':
                menu_reports(pro)
            elif c == '10':
                menu_settings(pro)
            elif c == '11':
                menu_help()
            elif c == 'a':
                menu_account_manage()
            elif c == 'w':
                run_web()
            elif c == '0':
                print("Bye~")
                break
            if c in ('u', '1', '2', '3', '4', '5', '6'):
                print_job_summary(pro)
        except Exception as e:
            logger.error(f"运行出错: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    sys.exit(main())
