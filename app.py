# -*- coding: utf-8 -*-

import io
import os
import base64
import uuid
import urllib.parse
from datetime import datetime, date, time, timezone, timedelta

import pandas as pd
import psycopg2
import streamlit as st
from passlib.hash import bcrypt
from PIL import Image
from openpyxl.worksheet.datavalidation import DataValidation

st.set_page_config(page_title="团委学生会物资管理", layout="wide")

CATEGORIES = ["办公用品", "活动物资", "宣传用品", "奖品/证书", "借用物资", "其他"]
TAB_ROLES = {
    "📝 录入出入库": ["operator", "admin"],
    "🔍 查询与导出": ["operator", "admin"],
    "📷 物资照片": ["operator", "admin"],
    "📅 按学期查询": ["operator", "admin"],
    "⚙️ 学期管理": ["admin"],
    "🔔 预警设置": ["operator", "admin"],
    "👥 用户管理": ["admin"],
    "🔑 修改密码": ["operator", "admin"],
}


def get_conn():
    return psycopg2.connect(
        host=st.secrets["DB_HOST"], port=st.secrets["DB_PORT"],
        dbname=st.secrets["DB_NAME"], user=st.secrets["DB_USER"],
        password=st.secrets["DB_PASSWORD"], connect_timeout=10
    )


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_log (
            id SERIAL PRIMARY KEY, item_name TEXT NOT NULL, category TEXT DEFAULT '其他',
            change_type TEXT NOT NULL CHECK(change_type IN ('IN','OUT')),
            quantity REAL NOT NULL CHECK(quantity > 0), log_time TEXT NOT NULL,
            note TEXT, operator TEXT, batch_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("ALTER TABLE stock_log ADD COLUMN IF NOT EXISTS batch_id TEXT")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS semesters (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            start_date TEXT NOT NULL, end_date TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_alert (
            item_name TEXT PRIMARY KEY, min_quantity REAL NOT NULL DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_users (
            id SERIAL PRIMARY KEY, email VARCHAR(255) UNIQUE NOT NULL,
            name VARCHAR(100), student_id VARCHAR(50),
            password_hash TEXT NOT NULL, role VARCHAR(20) NOT NULL DEFAULT 'operator',
            password_change_count INT DEFAULT 0,
            is_permanent_admin BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS name VARCHAR(100)")
    cur.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS student_id VARCHAR(50)")
    cur.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS password_change_count INT DEFAULT 0")
    cur.execute("ALTER TABLE app_users ADD COLUMN IF NOT EXISTS is_permanent_admin BOOLEAN DEFAULT FALSE")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS item_photos (
            item_name TEXT PRIMARY KEY,
            photo_base64 TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("SELECT COUNT(*) FROM semesters")
    if cur.fetchone()[0] == 0:
        y = date.today().year
        rows = [
            (f"{y-1}-{y}学年第一学期", f"{y-1}-09-01", f"{y}-01-31"),
            (f"{y-1}-{y}学年第二学期", f"{y}-02-15", f"{y}-07-15"),
            (f"{y}-{y+1}学年第一学期", f"{y}-09-01", f"{y+1}-01-31"),
            (f"{y}-{y+1}学年第二学期", f"{y+1}-02-15", f"{y+1}-07-15"),
        ]
        for row in rows:
            cur.execute("INSERT INTO semesters(name, start_date, end_date) VALUES (%s,%s,%s) ON CONFLICT(name) DO NOTHING", row)

    cur.execute("SELECT COUNT(*) FROM app_users")
    if cur.fetchone()[0] == 0:
        default_email = st.secrets.get("DEFAULT_ADMIN_EMAIL", "admin@example.com")
        default_password = st.secrets.get("DEFAULT_ADMIN_PASSWORD", "admin123456")
        cur.execute(
            "INSERT INTO app_users (email, password_hash, role, password_change_count, is_permanent_admin) VALUES (%s,%s,%s,%s,%s)",
            (default_email, bcrypt.hash(default_password), "admin", 0, True)
        )

    cur.execute("UPDATE app_users SET role = 'operator' WHERE role IN ('editor', 'viewer')")
    cur.execute("SELECT email FROM app_users WHERE role = 'admin' AND is_permanent_admin = FALSE ORDER BY created_at")
    normal_admins = cur.fetchall()
    if len(normal_admins) > 1:
        for (email,) in normal_admins[1:]:
            cur.execute("UPDATE app_users SET role = 'operator' WHERE email = %s", (email,))
    conn.commit()
    cur.close()
    conn.close()
if "selected_photo_item" not in st.session_state:
    st.session_state["selected_photo_item"] = None

try:
    init_db()
except Exception as e:
    st.error(f"数据库初始化失败：{e}")
    st.stop()


# ==================== 照片相关 ====================
def compress_image(file_bytes, max_size=800, quality=75):
    try:
        img = Image.open(io.BytesIO(file_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > max_size:
            if w >= h:
                new_w = max_size
                new_h = int(h * max_size / w)
            else:
                new_h = max_size
                new_w = int(w * max_size / h)
            img = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        return None


def upsert_item_photo(item_name, photo_base64):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO item_photos(item_name, photo_base64, updated_at)
        VALUES (%s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT(item_name) DO UPDATE SET
            photo_base64 = EXCLUDED.photo_base64,
            updated_at = CURRENT_TIMESTAMP
    """, (item_name, photo_base64))
    conn.commit()
    cur.close()
    conn.close()


def get_item_photo(item_name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT photo_base64 FROM item_photos WHERE item_name = %s", (item_name,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None


def get_photos_for_items(item_names):
    if not item_names:
        return {}
    conn = get_conn()
    cur = conn.cursor()
    placeholders = ",".join(["%s"] * len(item_names))
    cur.execute(f"SELECT item_name, photo_base64 FROM item_photos WHERE item_name IN ({placeholders})", list(item_names))
    result = {row[0]: row[1] for row in cur.fetchall()}
    cur.close()
    conn.close()
    return result


def get_all_photos():
    conn = get_conn()
    df = pd.read_sql_query("SELECT item_name AS 物品名称, photo_base64 AS 照片, updated_at AS 更新时间 FROM item_photos ORDER BY item_name", conn)
    conn.close()
    return df


def delete_item_photo(item_name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM item_photos WHERE item_name = %s", (item_name,))
    conn.commit()
    cur.close()
    conn.close()


def get_recent_logs(item_name, limit=3):
    """获取某物资最近 N 条出入库记录"""
    conn = get_conn()
    df = pd.read_sql_query("""
        SELECT log_time AS 时间,
               CASE change_type WHEN 'IN' THEN '入库' ELSE '出库' END AS 类型,
               quantity AS 数量,
               operator AS 操作人,
               note AS 备注
        FROM stock_log
        WHERE item_name = %s
        ORDER BY id DESC
        LIMIT %s
    """, conn, params=(item_name, limit))
    conn.close()
    return df

def render_photo_wall(items_with_photos, key_prefix="wall"):
    if not items_with_photos:
        return
    cols = st.columns(3)
    for idx, (item_name, photo_b64) in enumerate(items_with_photos):
        col = cols[idx % 3]
        with col:
            if photo_b64:
                try:
                    img_bytes = base64.b64decode(photo_b64)
                    st.image(img_bytes, use_container_width=True)
                except Exception:
                    st.warning("照片加载失败")
            st.markdown(f"**{item_name}**")
            if st.button("🔍 查看详情", key=f"{key_prefix}_view_{idx}", use_container_width=True):
                st.session_state["selected_photo_item"] = item_name
                st.rerun()
    
# ==================== 用户管理 ====================
def get_normal_admin_name():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT name, email FROM app_users WHERE role = 'admin' AND is_permanent_admin = FALSE LIMIT 1")
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        if row[0]:
            return row[0]
        else:
            return row[1]
    return None


def render_contact_html():
    admin_name = get_normal_admin_name()
    if admin_name:
        text = (
            f'有问题咨询 <strong>{admin_name}</strong>，如 <strong>{admin_name}</strong> 无法解决，'
            f'请联系牢大，QQ：1018833924'
        )
        return f'<p style="color:#1E90FF; font-size:15px; font-weight:bold;">{text}</p>'
    else:
        line1 = '请联系牢大，QQ：1018833924'
        line2 = '并提醒牢大给负责的部长设置为Admin'
        return (
            f'<p style="color:#1E90FF; font-size:15px; font-weight:bold; margin:0;">{line1}</p>'
            f'<p style="color:#1E90FF; font-size:18px; font-weight:bold; margin:4px 0 0 0;">{line2}</p>'
        )


def render_forgot_password_html():
    admin_name = get_normal_admin_name()
    if admin_name:
        line1 = f'如忘记你的密码，请联系 <strong>{admin_name}</strong> 重置密码'
        line2 = f'如 <strong>{admin_name}</strong> 无法解决，请联系牢大，QQ：1018833924'
        return (
            f'{line1}<br>'
            f'<span style="color:#1E90FF; font-size:12px;">{line2}</span>'
        )
    else:
        line1 = '请联系牢大，QQ：1018833924'
        line2 = '并提醒牢大给负责的部长设置为Admin'
        return (
            f'<p style="color:#1E90FF; font-size:15px; font-weight:bold; margin:0;">{line1}</p>'
            f'<p style="color:#1E90FF; font-size:18px; font-weight:bold; margin:4px 0 0 0;">{line2}</p>'
        )


def get_user(identifier):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT email, password_hash, role, name, student_id, is_permanent_admin FROM app_users WHERE email = %s OR student_id = %s",
        (identifier, identifier)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row


def create_user(email, name, student_id):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO app_users (email, name, student_id, password_hash, role, password_change_count, is_permanent_admin) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (email, name, student_id, bcrypt.hash("123456"), "operator", 0, False)
        )
        conn.commit()
        return True, "添加成功！初始密码为：123456"
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return False, "该邮箱已存在"
    finally:
        cur.close()
        conn.close()


def update_user_profile(email, name, student_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE app_users SET name = %s, student_id = %s WHERE email = %s", (name, student_id, email))
    conn.commit()
    cur.close()
    conn.close()


def transfer_admin(from_email, to_email):
    if from_email == to_email:
        return False, "不能转让给自己"
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT role, is_permanent_admin FROM app_users WHERE email = %s", (from_email,))
        from_row = cur.fetchone()
        if not from_row:
            return False, "系统错误：找不到您的账号"
        if from_row[1]:
            return False, "您是永久 Admin，不能通过此方式转让。"
        cur.execute("SELECT role FROM app_users WHERE email = %s", (to_email,))
        row = cur.fetchone()
        if not row:
            return False, "目标用户不存在"
        if row[0] == "admin":
            return False, "目标用户已是管理员"
        cur.execute("UPDATE app_users SET role = 'operator', is_permanent_admin = FALSE WHERE email = %s", (from_email,))
        cur.execute("UPDATE app_users SET role = 'admin', is_permanent_admin = FALSE WHERE email = %s", (to_email,))
        conn.commit()
        return True, f"已将管理员权限转让给 {to_email}，您现在已降为 Operator"
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        cur.close()
        conn.close()


def set_normal_admin(target_email):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT is_permanent_admin FROM app_users WHERE email = %s", (st.session_state.user,))
        op_row = cur.fetchone()
        if not op_row or not op_row[0]:
            return False, "只有永久 Admin 可以执行此操作"
        cur.execute("SELECT role, is_permanent_admin FROM app_users WHERE email = %s", (target_email,))
        target_row = cur.fetchone()
        if not target_row:
            return False, "目标用户不存在"
        if target_row[1]:
            return False, "目标用户已是永久 Admin，无需再设为普通 Admin"
        if target_row[0] == "admin" and not target_row[1]:
            return False, "目标用户已是普通 Admin"
        cur.execute("UPDATE app_users SET role = 'operator' WHERE role = 'admin' AND is_permanent_admin = FALSE")
        cur.execute("UPDATE app_users SET role = 'admin', is_permanent_admin = FALSE WHERE email = %s", (target_email,))
        conn.commit()
        return True, f"✅ 已将 {target_email} 设为普通 Admin（原普通 Admin 已自动降级为 Operator）"
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        cur.close()
        conn.close()


def reset_user_password(email):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE app_users SET password_hash = %s WHERE email = %s",
                (bcrypt.hash("123456"), email))
    conn.commit()
    cur.close()
    conn.close()


def delete_user(email):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM app_users WHERE email = %s", (email,))
    conn.commit()
    cur.close()
    conn.close()


def list_users(keyword=""):
    conn = get_conn()
    sql = ('SELECT email AS 邮箱, name AS 姓名, student_id AS 学号, role AS 角色, '
           'is_permanent_admin AS "永久Admin", created_at AS 创建时间 FROM app_users')
    params = []
    if keyword and keyword.strip():
        sql += " WHERE name ILIKE %s OR student_id ILIKE %s OR email ILIKE %s "
        kw = f"%{keyword.strip()}%"
        params = [kw, kw, kw]
    sql += " ORDER BY created_at"
    df = pd.read_sql_query(sql, conn, params=params if params else None)
    conn.close()
    return df


def render_user_table(df):
    html = '<table style="width:100%; border-collapse: collapse; font-family: sans-serif; font-size: 14px;">'
    html += '<thead><tr style="background-color:#4A6FA5; color:white;">'
    for col in ["邮箱", "姓名", "学号", "身份", "创建时间"]:
        html += f'<th style="padding:10px; text-align:left; border:1px solid #ddd;">{col}</th>'
    html += '</tr></thead><tbody>'
    for _, row in df.iterrows():
        is_perm = bool(row["永久Admin"])
        role = row["角色"]
        if is_perm:
            role_text = "Admin"
            role_style = "color:#DAA520; font-weight:bold;"
        elif role == "admin":
            role_text = "Admin"
            role_style = "color:white;"
        else:
            role_text = "Operator"
            role_style = "color:white;"
        email_v = row["邮箱"] or ""
        name_v = row["姓名"] or ""
        sid_v = row["学号"] or ""
        ctime_v = str(row["创建时间"]) if row["创建时间"] is not None else ""
        html += '<tr style="background-color:#262730; color:white;">'
        html += f'<td style="padding:8px; border:1px solid #ddd;">{email_v}</td>'
        html += f'<td style="padding:8px; border:1px solid #ddd;">{name_v}</td>'
        html += f'<td style="padding:8px; border:1px solid #ddd;">{sid_v}</td>'
        html += f'<td style="padding:8px; border:1px solid #ddd; {role_style}">{role_text}</td>'
        html += f'<td style="padding:8px; border:1px solid #ddd;">{ctime_v}</td>'
        html += '</tr>'
    html += '</tbody></table>'
    return html


def change_user_password(current_login_email, input_email, input_name, input_student_id, old_password, new_password):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT email, name, student_id, password_hash FROM app_users WHERE email = %s", (current_login_email,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return False, "系统错误：用户不存在"
    db_email, db_name, db_student_id, db_password_hash = row
    if input_email.strip() != db_email:
        cur.close()
        conn.close()
        return False, "❌ 输入的邮箱与当前登录账号不匹配"
    if input_name.strip() != db_name:
        cur.close()
        conn.close()
        return False, "❌ 输入的姓名与系统记录不匹配"
    if input_student_id.strip() != db_student_id:
        cur.close()
        conn.close()
        return False, "❌ 输入的学号与系统记录不匹配"
    if not bcrypt.verify(old_password, db_password_hash):
        cur.close()
        conn.close()
        return False, "❌ 旧密码错误"
    cur.execute("UPDATE app_users SET password_hash = %s WHERE email = %s",
                (bcrypt.hash(new_password), current_login_email))
    conn.commit()
    cur.close()
    conn.close()
    return True, "✅ 密码修改成功，请使用新密码登录"


# ==================== 出入库 ====================
def insert_log(item_name, category, change_type, quantity, log_time, note, operator):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO stock_log(item_name, category, change_type, quantity, log_time, note, operator) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (item_name, category, change_type, quantity, log_time, note, operator))
    conn.commit()
    cur.close()
    conn.close()


def get_stock(item_name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(CASE WHEN change_type='IN' THEN quantity ELSE -quantity END), 0) FROM stock_log WHERE item_name = %s", (item_name,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else 0


def _setup_template_sheet(ws):
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter

    header_font = Font(bold=True, color="FFFFFF", size=12)
    header_fill = PatternFill("solid", fgColor="4A6FA5")
    sub_font = Font(bold=True)
    sub_fill = PatternFill("solid", fgColor="D9E1F2")
    center = Alignment(horizontal="center", vertical="center")
    thin = Side(style="thin", color="B0B0B0")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for idx, cat in enumerate(CATEGORIES):
        col_start = idx * 3 + 1
        cell = ws.cell(row=1, column=col_start, value=cat)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
        cell.border = border
        ws.merge_cells(start_row=1, start_column=col_start, end_row=1, end_column=col_start + 2)
        for c in range(col_start, col_start + 3):
            ws.cell(row=1, column=c).border = border
        for j, h in enumerate(["物品名称", "数量", "备注"]):
            c = ws.cell(row=2, column=col_start + j, value=h)
            c.font = sub_font
            c.fill = sub_fill
            c.alignment = center
            c.border = border
        ws.column_dimensions[get_column_letter(col_start)].width = 18
        ws.column_dimensions[get_column_letter(col_start + 1)].width = 8
        ws.column_dimensions[get_column_letter(col_start + 2)].width = 16
    ws.row_dimensions[1].height = 22
    ws.row_dimensions[2].height = 18


def generate_import_template():
    from openpyxl import Workbook
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "入库"
    _setup_template_sheet(ws1)
    ws2 = wb.create_sheet("出库")
    _setup_template_sheet(ws2)
    output = io.BytesIO()
    wb.save(output)
    return output.getvalue()


def parse_excel_import(file_bytes):
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(file_bytes), data_only=True)
    except Exception as e:
        return [], [f"无法读取 Excel：{e}"]

    rows = []
    errors = []
    sheet_map = {"入库": "IN", "出库": "OUT"}

    for sheet_name, change_type in sheet_map.items():
        if sheet_name not in wb.sheetnames:
            errors.append(f"缺少 Sheet：{sheet_name}")
            continue
        ws = wb[sheet_name]
        for idx, cat in enumerate(CATEGORIES):
            col_item = idx * 3 + 1
            col_qty = idx * 3 + 2
            col_note = idx * 3 + 3
            for r in range(3, ws.max_row + 1):
                item_cell = ws.cell(row=r, column=col_item).value
                if item_cell is None or str(item_cell).strip() == "":
                    continue
                item = str(item_cell).strip()
                qty_cell = ws.cell(row=r, column=col_qty).value
                note_cell = ws.cell(row=r, column=col_note).value
                try:
                    qty = int(float(qty_cell)) if qty_cell is not None else None
                except (TypeError, ValueError):
                    qty = None
                if qty is None or qty <= 0:
                    errors.append(f"[{sheet_name}] {cat} 第 {r} 行「{item}」：数量无效")
                    continue
                note_val = "" if note_cell is None else str(note_cell).strip()
                rows.append({
                    "sheet": sheet_name,
                    "change_type": change_type,
                    "category": cat,
                    "item_name": item,
                    "quantity": qty,
                    "note": note_val,
                })
    return rows, errors


def import_logs_from_parsed(rows, fixed_operator, fixed_date):
    success = 0
    errors = []
    batch_id = uuid.uuid4().hex
    conn = get_conn()
    cur = conn.cursor()
    for r in rows:
        try:
            item = r["item_name"]
            cat = r["category"]
            qty = r["quantity"]
            note_val = r["note"]
            change_type = r["change_type"]
            log_time = f"{fixed_date} 00:00:00"
            if change_type == "OUT":
                cur.execute("SELECT COALESCE(SUM(CASE WHEN change_type='IN' THEN quantity ELSE -quantity END), 0) FROM stock_log WHERE item_name = %s", (item,))
                stock = cur.fetchone()[0]
                if stock < qty:
                    errors.append(f"{item} 库存不足，请确认后再输入！")
                    continue
            cur.execute(
                "INSERT INTO stock_log(item_name, category, change_type, quantity, log_time, note, operator, batch_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (item, cat, change_type, qty, log_time, note_val, fixed_operator, batch_id)
            )
            success += 1
        except Exception as e:
            errors.append(f"[{r['sheet']}] {r['category']}「{r['item_name']}」：{e}")
    conn.commit()
    cur.close()
    conn.close()
    return success, errors, batch_id


def undo_batch(batch_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM stock_log WHERE batch_id = %s", (batch_id,))
    count = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return count


def delete_log_by_id(log_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM stock_log WHERE id = %s", (log_id,))
    count = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return count


def list_all_logs(limit=200):
    conn = get_conn()
    df = pd.read_sql_query(f"""
        SELECT id, log_time AS 时间, item_name AS 物品名称, category AS 类别,
               CASE change_type WHEN 'IN' THEN '入库' ELSE '出库' END AS 类型,
               quantity AS 数量, operator AS 操作人, note AS 备注,
               batch_id AS 批次
        FROM stock_log ORDER BY id DESC LIMIT {int(limit)}
    """, conn)
    conn.close()
    return df


def query_stock(keyword="", category="全部"):
    conn = get_conn()
    sql = """
        SELECT sl.item_name AS 物品名称, MAX(sl.category) AS 类别,
               SUM(CASE WHEN sl.change_type='IN'  THEN sl.quantity ELSE 0 END) AS 累计入库,
               SUM(CASE WHEN sl.change_type='OUT' THEN sl.quantity ELSE 0 END) AS 累计出库,
               SUM(CASE WHEN sl.change_type='IN'  THEN sl.quantity ELSE -sl.quantity END) AS 当前库存,
               MAX(sa.min_quantity) AS 预警阈值
        FROM stock_log sl LEFT JOIN stock_alert sa ON sl.item_name = sa.item_name
        WHERE sl.item_name LIKE %s
    """
    params = [f"%{keyword}%"]
    if category and category != "全部":
        sql += " AND sl.category = %s"
        params.append(category)
    sql += " GROUP BY sl.item_name ORDER BY sl.item_name"
    df = pd.read_sql_query(sql, conn, params=params)
    conn.close()
    if not df.empty:
        df["状态"] = df.apply(lambda r: "⚠️ 库存不足" if pd.notna(r["预警阈值"]) and r["当前库存"] < r["预警阈值"] else "✅ 正常", axis=1)
    return df


def query_stock_only(keyword="", category="全部"):
    df = query_stock(keyword, category)
    if df.empty:
        return df
    return df[["物品名称", "类别", "当前库存", "预警阈值", "状态"]]


def query_logs(keyword, start_dt, end_dt, category="全部"):
    conn = get_conn()
    sql = """
        SELECT log_time AS 时间, item_name AS 物品名称, category AS 类别,
               CASE change_type WHEN 'IN' THEN '入库' ELSE '出库' END AS 类型,
               quantity AS 数量, operator AS 操作人, note AS 备注
        FROM stock_log WHERE item_name LIKE %s AND log_time BETWEEN %s AND %s
    """
    params = [f"%{keyword}%", start_dt, end_dt]
    if category and category != "全部":
        sql += " AND category = %s"
        params.append(category)
    sql += " ORDER BY log_time DESC"
    df = pd.read_sql_query(sql, conn, params=params)
    conn.close()
    return df


def query_summary(keyword, start_dt, end_dt, category="全部"):
    conn = get_conn()
    sql = """
        SELECT item_name AS 物品名称, MAX(category) AS 类别,
               SUM(CASE WHEN log_time < %s THEN CASE WHEN change_type='IN' THEN quantity ELSE -quantity END ELSE 0 END) AS 期初库存,
               SUM(CASE WHEN log_time BETWEEN %s AND %s AND change_type='IN' THEN quantity ELSE 0 END) AS 期间入库,
               SUM(CASE WHEN log_time BETWEEN %s AND %s AND change_type='OUT' THEN quantity ELSE 0 END) AS 期间出库,
               SUM(CASE WHEN log_time <= %s THEN CASE WHEN change_type='IN' THEN quantity ELSE -quantity END ELSE 0 END) AS 期末库存
        FROM stock_log WHERE item_name LIKE %s
    """
    params = [start_dt, start_dt, end_dt, start_dt, end_dt, end_dt, f"%{keyword}%"]
    if category and category != "全部":
        sql += " AND category = %s"
        params.append(category)
    sql += " GROUP BY item_name ORDER BY item_name"
    df = pd.read_sql_query(sql, conn, params=params)
    conn.close()
    if not df.empty:
        df = df[df[["期初库存", "期间入库", "期间出库", "期末库存"]].abs().sum(axis=1) > 0]
    return df


def query_ranking(start_dt, end_dt, top_n=10):
    conn = get_conn()
    sql = "SELECT item_name AS 物品名称, SUM(quantity) AS 出库数量 FROM stock_log WHERE change_type='OUT' AND log_time BETWEEN %s AND %s GROUP BY item_name ORDER BY 出库数量 DESC LIMIT %s"
    df = pd.read_sql_query(sql, conn, params=(start_dt, end_dt, top_n))
    conn.close()
    return df


def get_all_items():
    conn = get_conn()
    df = pd.read_sql_query("SELECT DISTINCT item_name FROM stock_log ORDER BY item_name", conn)
    conn.close()
    return df["item_name"].tolist()


def get_alerts():
    conn = get_conn()
    df = pd.read_sql_query("""
        SELECT sa.item_name AS 物品名称, sa.min_quantity AS 预警阈值, sa.updated_at AS 更新时间,
               COALESCE((SELECT SUM(CASE WHEN change_type='IN' THEN quantity ELSE -quantity END) FROM stock_log WHERE item_name = sa.item_name), 0) AS 当前库存
        FROM stock_alert sa ORDER BY sa.item_name
    """, conn)
    conn.close()
    if not df.empty:
        df["状态"] = df.apply(lambda r: "⚠️ 库存不足" if r["当前库存"] < r["预警阈值"] else "✅ 正常", axis=1)
    return df


def set_alert(item_name, min_quantity):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO stock_alert(item_name, min_quantity, updated_at) VALUES (%s, %s, CURRENT_TIMESTAMP) ON CONFLICT(item_name) DO UPDATE SET min_quantity = EXCLUDED.min_quantity, updated_at = CURRENT_TIMESTAMP", (item_name, min_quantity))
    conn.commit()
    cur.close()
    conn.close()


def delete_alert(item_name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM stock_alert WHERE item_name=%s", (item_name,))
    conn.commit()
    cur.close()
    conn.close()


def get_semesters():
    conn = get_conn()
    df = pd.read_sql_query("SELECT id, name AS 学期名称, start_date AS 开始日期, end_date AS 结束日期 FROM semesters ORDER BY start_date DESC", conn)
    conn.close()
    return df


def add_semester(name, start_date, end_date):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO semesters(name, start_date, end_date) VALUES (%s,%s,%s)", (name, start_date, end_date))
        conn.commit()
        return True, "添加成功"
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return False, "学期名称已存在"
    finally:
        cur.close()
        conn.close()


def delete_semester(sid):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM semesters WHERE id=%s", (sid,))
    conn.commit()
    cur.close()
    conn.close()


def get_default_semester(semesters_df):
    today = date.today().strftime("%Y-%m-%d")
    for _, row in semesters_df.iterrows():
        if row["开始日期"] <= today <= row["结束日期"]:
            return row["学期名称"]
    return semesters_df.iloc[0]["学期名称"] if not semesters_df.empty else None


def _prepare_stock_df(keyword="", category="全部"):
    df = query_stock(keyword, category)
    if df.empty:
        return df
    return df[["物品名称", "类别", "累计入库", "累计出库", "当前库存", "预警阈值", "状态"]]


def _register_chinese_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    candidates = [
        ("SimHei", "C:/Windows/Fonts/simhei.ttf"),
        ("MicrosoftYaHei", "C:/Windows/Fonts/msyh.ttf"),
        ("SimSun", "C:/Windows/Fonts/simsun.ttf"),
        ("PingFang", "/System/Library/Fonts/PingFang.ttc"),
        ("WQY", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc")
    ]
    for name, path in candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont(name, path))
                return name
            except Exception:
                continue
    return "Helvetica"


def export_multi_items_excel(detail_df, selected_items):
    if not selected_items:
        return None
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for item in selected_items:
            sub = detail_df[detail_df["物品名称"] == item]
            if sub.empty:
                continue
            sheet_name = str(item)[:31].replace("/", "_").replace("\\", "_").replace("?", "_").replace("*", "_").replace("[", "_").replace("]", "_")
            sub.to_excel(writer, sheet_name=sheet_name, index=False)
    return output.getvalue()


def export_multi_items_pdf(detail_df, selected_items):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, PageBreak

    if not selected_items:
        return None
    font_name = _register_chinese_font()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=12*mm, rightMargin=12*mm, topMargin=12*mm, bottomMargin=12*mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("T", parent=styles["Title"], fontName=font_name, fontSize=16, leading=22)
    normal_style = ParagraphStyle("N", parent=styles["Normal"], fontName=font_name, fontSize=9, leading=12)

    elements = []

    def df_to_table(df, font_size=8):
        data = [list(df.columns)] + df.fillna("").astype(str).values.tolist()
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, -1), font_name),
            ("FONTSIZE", (0, 0), (-1, -1), font_size),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4A6FA5")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F5FA")]),
        ]))
        return t

    for idx, item in enumerate(selected_items):
        sub = detail_df[detail_df["物品名称"] == item]
        if sub.empty:
            continue
        if idx > 0:
            elements.append(PageBreak())
        elements.append(Paragraph(f"物资出入库记录：{item}", title_style))
        elements.append(Spacer(1, 4*mm))
        elements.append(Paragraph(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", normal_style))
        elements.append(Spacer(1, 4*mm))
        elements.append(df_to_table(sub, font_size=8))
    doc.build(elements)
    return buffer.getvalue()


# ==================== 登录 ====================
if "user" not in st.session_state:
    st.session_state.user = None
    st.session_state.role = None
    st.session_state.name = None
    st.session_state.student_id = None

if st.session_state.user is None:
    st.title("📦 团委学生会物资管理系统")

    st.subheader("请登录")
    st.markdown(render_contact_html(), unsafe_allow_html=True)
    with st.form("login_form"):
        identifier = st.text_input("邮箱或学号")
        password = st.text_input("密码", type="password")
        if st.form_submit_button("登录", type="primary"):
            if not identifier or not password:
                st.error("请填写账号和密码")
            else:
                user = get_user(identifier.strip())
                if user and bcrypt.verify(password, user[1]):
                    st.session_state.user = user[0]
                    st.session_state.role = user[2]
                    st.session_state.name = user[3]
                    st.session_state.student_id = user[4]
                    st.rerun()
                else:
                    st.error("账号或密码错误")

    st.stop()

if not st.session_state.name or not st.session_state.student_id:
    st.title("📦 团委学生会物资管理系统")
    st.subheader("首次登录，请完善个人信息")
    with st.form("complete_profile"):
        col1, col2 = st.columns(2)
        with col1:
            name = st.text_input("姓名 *")
        with col2:
            student_id = st.text_input("学号 *")
        if st.form_submit_button("确认", type="primary"):
            if not name.strip():
                st.error("姓名不能为空")
            elif not student_id.strip():
                st.error("学号不能为空")
            else:
                update_user_profile(st.session_state.user, name.strip(), student_id.strip())
                st.session_state.name = name.strip()
                st.session_state.student_id = student_id.strip()
                st.rerun()
    st.stop()

st.title("📦 团委学生会物资管理系统")
st.sidebar.markdown(f"**当前用户**：{st.session_state.name}")
st.sidebar.markdown(f"**学号**：{st.session_state.student_id}")
st.sidebar.markdown(f"**邮箱**：{st.session_state.user}")
_role_label = "Admin（管理员）" if st.session_state.role == "admin" else "Operator（操作员）"
st.sidebar.markdown(f"**角色**：{_role_label}")
if st.sidebar.button("退出登录"):
    st.session_state.user = None
    st.session_state.role = None
    st.session_state.name = None
    st.session_state.student_id = None
    st.rerun()

# ===== 全局：撤回导入按钮（3 分钟内有效） =====
if "last_batch" in st.session_state and st.session_state.last_batch:
    batch_info = st.session_state.last_batch
    elapsed = (datetime.now() - batch_info["time"]).total_seconds()
    if elapsed < 180:
        remaining = int(180 - elapsed)
        col_a, col_b = st.columns([3, 1])
        with col_a:
            st.info(f"📥 您刚才导入了 {batch_info['count']} 条记录，可在 {remaining} 秒内撤回。")
        with col_b:
            if st.button("↩️ 撤回本次导入", type="primary", key="undo_batch_btn"):
                count = undo_batch(batch_info["id"])
                st.success(f"已撤回 {count} 条记录。")
                st.session_state.last_batch = None
                st.rerun()
    else:
        st.session_state.last_batch = None

_alerts = get_alerts()
if not _alerts.empty:
    _short = _alerts[_alerts["状态"] == "⚠️ 库存不足"]
    if not _short.empty:
        st.warning(f"⚠️ 当前有 {len(_short)} 种物品库存低于预警阈值，请及时补充。")

role = st.session_state.role
allowed = [name for name, roles in TAB_ROLES.items() if role in roles]
tab_objs = st.tabs(allowed)
tab_dict = dict(zip(allowed, tab_objs))


# ==================== Tab：录入出入库 ====================
if "📝 录入出入库" in tab_dict:
    with tab_dict["📝 录入出入库"]:
        st.subheader("录入出入库")
        mode = st.radio("录入方式", ["✍️ 单条录入", "📥 批量导入 Excel"], horizontal=True, key="entry_mode")

        tz = timezone(timedelta(hours=8))
        now_bj = datetime.now(tz)
        today_bj_str = now_bj.strftime("%Y-%m-%d")

        if mode == "✍️ 单条录入":
            existing_items = get_all_items()
            with st.form("entry_form", clear_on_submit=True):
                col1, col2 = st.columns(2)
                with col1:
                    if existing_items:
                        item_name = st.selectbox("物品名称 *", options=existing_items, index=None, accept_new_options=True, placeholder="输入首字即可搜索，或输入新物品", key="item_select")
                    else:
                        item_name = st.text_input("物品名称 *（暂无历史物品）")
                    category = st.selectbox("类别 *", CATEGORIES)
                with col2:
                    change_type_cn = st.selectbox("类型 *", ["入库", "出库"])
                    quantity = st.number_input("数量 *", min_value=1, step=1, value=1, format="%d")

                use_single_custom_date = st.checkbox("📅 使用自定义日期", value=False, key="single_use_custom_date")
                if use_single_custom_date:
                    single_date = st.date_input("选择日期", value=date.today(), key="single_custom_date")
                    single_final_date = single_date.strftime("%Y-%m-%d")
                    single_date_source = "自定义"
                else:
                    single_final_date = today_bj_str
                    single_date_source = "北京时间"

                st.info(f"📌 操作人：{st.session_state.name}　|　📅 日期：{single_final_date}（{single_date_source}）")

                photo_file = st.file_uploader(
                    "📷 物资照片（可选，手机可直接拍照或从相册选择）",
                    type=["jpg", "jpeg", "png", "webp"],
                    key="entry_photo"
                )
                if photo_file is not None:
                    st.image(photo_file, caption="照片预览", width=200)

                col3, col4 = st.columns(2)
                with col3:
                    note = st.text_input("备注")

                if st.form_submit_button("提交", type="primary"):
                    errors = []
                    if not item_name or not str(item_name).strip():
                        errors.append("物品名称不能为空")

                    if errors:
                        for e in errors:
                            st.error(e)
                    else:
                        log_time = f"{single_final_date} 00:00:00"
                        change_type = "IN" if change_type_cn == "入库" else "OUT"
                        clean_name = str(item_name).strip()
                        clean_operator = st.session_state.name

                        if change_type == "OUT":
                            stock = get_stock(clean_name)
                            if stock < quantity:
                                st.error(f"{clean_name} 库存不足，请确认后再输入！")
                            else:
                                insert_log(clean_name, category, change_type, quantity, log_time, note, clean_operator)
                                if photo_file is not None:
                                    b64 = compress_image(photo_file.getvalue())
                                    if b64:
                                        upsert_item_photo(clean_name, b64)
                                st.success(f"✅ 已录入出库：{clean_name} × {quantity}（{single_final_date}）")
                        else:
                            insert_log(clean_name, category, change_type, quantity, log_time, note, clean_operator)
                            if photo_file is not None:
                                b64 = compress_image(photo_file.getvalue())
                                if b64:
                                    upsert_item_photo(clean_name, b64)
                            st.success(f"✅ 已录入入库：{clean_name} × {quantity}（{single_final_date}）")
        else:
            st.markdown("##### 📋 使用说明")
            st.markdown("""
            1. 点击下方按钮下载 Excel 模板
            2. 模板有两个 Sheet：**入库** 和 **出库**
            3. 每个 Sheet 中，各类别按列分开（办公用品 / 活动物资 / 宣传用品 / 奖品/证书 / 借用物资 / 其他）
            4. 每个类别下有三列：**物品名称**、**数量**、**备注**
            5. 从第 3 行开始填写数据，数量填写正整数
            6. 上传填好的 Excel 文件，确认导入。导入后 3 分钟内可以撤回
            7. 📷 **照片请在导入完成后到「📷 物资照片」标签单独上传**（Excel 单元格无法内嵌图片）
            """)

            col_d1, col_d2 = st.columns([1, 2])
            with col_d1:
                use_custom_date = st.checkbox("📅 使用自定义日期", value=False, key="use_custom_date")
            if use_custom_date:
                with col_d2:
                    custom_date = st.date_input("选择日期", value=date.today(), key="batch_custom_date")
                final_date = custom_date.strftime("%Y-%m-%d")
                date_source = "自定义"
            else:
                final_date = today_bj_str
                date_source = "北京时间"

            st.info(f"📌 本次导入的所有记录，操作人统一为：**{st.session_state.name}**　|　📅 日期统一为：**{final_date}**（{date_source}）")

            col1, col2 = st.columns(2)
            with col1:
                st.download_button("⬇️ 下载 Excel 模板", generate_import_template(), file_name="出入库导入模板.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            uploaded_file = st.file_uploader("上传填好的 Excel 文件（.xlsx）", type=["xlsx"], key="import_file")
            if uploaded_file is not None:
                try:
                    file_bytes = uploaded_file.read()
                    rows, parse_errors = parse_excel_import(file_bytes)

                    if parse_errors:
                        with st.expander(f"⚠️ 解析过程中发现 {len(parse_errors)} 个问题", expanded=True):
                            for e in parse_errors:
                                st.write(f"- {e}")

                    if not rows:
                        st.warning("文件中没有可导入的数据行。")
                    else:
                        st.markdown("##### 📄 数据预览")
                        preview_df = pd.DataFrame([
                            {
                                "Sheet": r["sheet"],
                                "类别": r["category"],
                                "物品名称": r["item_name"],
                                "数量": r["quantity"],
                                "备注": r["note"],
                            } for r in rows
                        ])
                        st.dataframe(preview_df, use_container_width=True, hide_index=True)
                        st.caption(f"共 {len(rows)} 条待导入")

                        if st.button("✅ 确认导入", type="primary", key="btn_import"):
                            with st.spinner("导入中..."):
                                success, errors, batch_id = import_logs_from_parsed(rows, st.session_state.name, final_date)
                            if success > 0:
                                st.session_state.last_batch = {
                                    "id": batch_id,
                                    "time": datetime.now(),
                                    "count": success,
                                }
                                st.success(f"✅ 成功导入 {success} 条记录（操作人：{st.session_state.name}，日期：{final_date}）")
                                st.info("⏱️ 3 分钟内可在页面顶部点击「撤回本次导入」撤销。")
                            if errors:
                                with st.expander(f"⚠️ 有 {len(errors)} 条未导入，点击查看原因"):
                                    for e in errors:
                                        st.write(f"- {e}")
                            if success > 0 and not errors:
                                st.balloons()
                except Exception as e:
                    st.error(f"处理文件失败：{e}")


# ==================== Tab：查询与导出 ====================
if "🔍 查询与导出" in tab_dict:
    with tab_dict["🔍 查询与导出"]:
        st.subheader("查询与导出")
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            keyword = st.text_input("物品名称关键词", "")
        with col2:
            category_filter = st.selectbox("类别筛选", ["全部"] + CATEGORIES)
        with col3:
            start_date = st.date_input("开始日期", value=date.today().replace(day=1))
        with col4:
            end_date = st.date_input("结束日期", value=date.today())

        only_stock = st.checkbox("🔎 只看库存（不显示时间段汇总和明细）", value=False)

        if st.button("查询", type="primary", key="btn_query"):
            start_dt = datetime.combine(start_date, time.min).strftime("%Y-%m-%d %H:%M:%S")
            end_dt = datetime.combine(end_date, time.max).strftime("%Y-%m-%d %H:%M:%S")

            if only_stock:
                st.markdown("### 📊 所有物品库存")
                stock_only_df = query_stock_only(keyword, category_filter)
                st.dataframe(stock_only_df, use_container_width=True, hide_index=True)

                if not stock_only_df.empty:
                    item_names = stock_only_df["物品名称"].tolist()
                    photos = get_photos_for_items(item_names)
                    items_with_photos = [(name, photos.get(name)) for name in item_names if photos.get(name)]
                    if items_with_photos:
                        st.markdown("### 📷 物资照片（点击缩略图查看详情）")
                        render_photo_wall(items_with_photos, key_prefix="query")
                    else:
                        st.caption("📷 暂无照片，可到「📷 物资照片」标签上传")
            else:
                st.markdown("### 📊 当前库存")
                stock_df = query_stock(keyword, category_filter)
                st.dataframe(stock_df, use_container_width=True, hide_index=True)

                if not stock_df.empty:
                    item_names = stock_df["物品名称"].tolist()
                    photos = get_photos_for_items(item_names)
                    items_with_photos = [(name, photos.get(name)) for name in item_names if photos.get(name)]
                    if items_with_photos:
                        st.markdown("### 📷 物资照片（点击缩略图查看详情）")
                        render_photo_wall(items_with_photos, key_prefix="query")

                st.markdown("### 📈 期间汇总")
                summary_df = query_summary(keyword, start_dt, end_dt, category_filter)
                st.dataframe(summary_df, use_container_width=True, hide_index=True)

                st.markdown("### 📋 出入库明细")
                logs_df = query_logs(keyword, start_dt, end_dt, category_filter)
                st.dataframe(logs_df, use_container_width=True, hide_index=True)

                if not logs_df.empty:
                    st.divider()
                    st.markdown("#### 📤 导出选中物资的出入库明细")

                    all_items = sorted(logs_df["物品名称"].unique().tolist())

                    col_a, col_b = st.columns([3, 1])
                    with col_a:
                        selected_items = st.multiselect("选择要导出的物资（可多选）", options=all_items, default=all_items, key="export_multi_items")
                    with col_b:
                        export_format = st.selectbox("导出格式", ["Excel (.xlsx)", "PDF (.pdf)"], key="multi_export_format")

                    if st.button("🚀 生成并下载", type="primary", key="btn_multi_export"):
                        if not selected_items:
                            st.warning("请至少选择一种物资。")
                        else:
                            if export_format.startswith("Excel"):
                                data = export_multi_items_excel(logs_df, selected_items)
                                if data:
                                    st.download_button("⬇️ 点击下载 Excel", data, file_name=f"物资出入库明细_{date.today()}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary")
                                    st.success("✅ Excel 已生成，每个物资一个 Sheet。")
                                else:
                                    st.error("没有可导出的数据。")
                            else:
                                data = export_multi_items_pdf(logs_df, selected_items)
                                if data:
                                    st.download_button("⬇️ 点击下载 PDF", data, file_name=f"物资出入库明细_{date.today()}.pdf", mime="application/pdf", type="primary")
                                    st.success("✅ PDF 已生成，每个物资单独一页。")
                                else:
                                    st.error("没有可导出的数据。")

                    st.divider()
                    st.markdown("#### 📤 导出全部库存汇总")
                    if st.button("📊 导出库存汇总表（Excel）", key="btn_export_stock_summary"):
                        stock_export_df = _prepare_stock_df(keyword, category_filter)
                        if stock_export_df.empty:
                            st.warning("没有可导出的库存数据。")
                        else:
                            output = io.BytesIO()
                            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                                stock_export_df.to_excel(writer, sheet_name="库存汇总", index=False)
                            st.download_button("⬇️ 点击下载 Excel", output.getvalue(), file_name=f"物资库存汇总_{date.today()}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary")

        # ===== Admin 删除任意记录 =====
        if st.session_state.role == "admin":
            st.divider()
            st.markdown("#### 🗑️ 删除记录（仅 Admin）")
            st.caption("选择任意一条记录进行删除，不限时间。删除后不可恢复。")

            all_logs_df = list_all_logs(limit=200)

            if all_logs_df.empty:
                st.info("暂无记录。")
            else:
                del_search = st.text_input("搜索物品名称（可选）", "", key="del_search", placeholder="输入物品名称关键词筛选")
                filtered_logs = all_logs_df
                if del_search.strip():
                    filtered_logs = all_logs_df[all_logs_df["物品名称"].astype(str).str.contains(del_search.strip(), case=False, na=False)]

                if filtered_logs.empty:
                    st.warning("没有匹配的记录。")
                else:
                    st.dataframe(filtered_logs[["时间", "物品名称", "类别", "类型", "数量", "操作人", "备注"]], use_container_width=True, hide_index=True)
                    st.caption(f"共 {len(filtered_logs)} 条记录（最多显示最近 200 条）")

                    display_options = [
                        f"[ID {row['id']}] {row['时间']} | {row['类型']} | {row['物品名称']} × {row['数量']} | 操作人：{row['操作人']}"
                        for _, row in filtered_logs.iterrows()
                    ]
                    selected_log_display = st.selectbox("选择要删除的记录", display_options, key="del_log_select")
                    selected_log_id = filtered_logs.iloc[display_options.index(selected_log_display)]["id"]

                    col_d1, col_d2 = st.columns([1, 3])
                    with col_d1:
                        if st.button("🗑️ 删除该记录", type="primary", key="btn_delete_log"):
                            count = delete_log_by_id(int(selected_log_id))
                            if count > 0:
                                st.success(f"已删除记录 ID {selected_log_id}。")
                                st.rerun()
                            else:
                                st.error("删除失败，记录可能已被删除。")


# ==================== Tab：物资照片 ====================
if "📷 物资照片" in tab_dict:
    with tab_dict["📷 物资照片"]:
        # ===== 详情模式 =====
        if st.session_state.get("selected_photo_item"):
            item_name = st.session_state["selected_photo_item"]

            if st.button("← 返回照片墙", key="photo_tab_back"):
                st.session_state["selected_photo_item"] = None
                st.rerun()

            st.markdown(f"### 📷 {item_name}")

            photo_b64 = get_item_photo(item_name)
            if not photo_b64:
                st.warning("该物资暂无照片。")

            col_left, col_right = st.columns([1, 1])

            with col_left:
                if photo_b64:
                    try:
                        img_bytes = base64.b64decode(photo_b64)
                        st.image(img_bytes, use_container_width=True)
                    except Exception:
                        st.warning("照片加载失败")
                    st.caption("提示：可右键图片 → 在新标签页打开图片查看原图。")

            with col_right:
                stock_qty = get_stock(item_name)
                st.metric(label="📦 当前库存", value=int(stock_qty) if stock_qty is not None else 0)

                st.markdown("##### 📋 最近 3 条出入库记录")
                recent_df = get_recent_logs(item_name, limit=3)
                if recent_df.empty:
                    st.info("该物资暂无出入库记录。")
                else:
                    st.dataframe(recent_df, use_container_width=True, hide_index=True)

        # ===== 照片墙模式 =====
        else:
            st.subheader("📷 物资照片管理")
            st.caption("点击「🔍 查看详情」可查看放大照片、当前库存和最近 3 条出入库记录。")

            existing_items = get_all_items()

            if not existing_items:
                st.info("暂无物资记录。请先到「📝 录入出入库」录入物资。")
            else:
                st.markdown("##### ⬆️ 上传 / 更新照片")
                col1, col2 = st.columns([1, 1])
                with col1:
                    photo_item = st.selectbox("选择物资", existing_items, key="photo_tab_item_select")
                with col2:
                    photo_file = st.file_uploader(
                        "📷 选择照片（手机可直接拍照或从相册选择）",
                        type=["jpg", "jpeg", "png", "webp"],
                        key="photo_tab_upload_file"
                    )

                if photo_file is not None:
                    st.image(photo_file, caption="照片预览", width=240)
                    if st.button("💾 保存照片", type="primary", key="photo_tab_btn_save"):
                        b64 = compress_image(photo_file.getvalue())
                        if b64:
                            upsert_item_photo(photo_item, b64)
                            st.success(f"✅ 已保存 {photo_item} 的照片")
                            st.rerun()
                        else:
                            st.error("照片处理失败，请重试或换一张图片。")

                st.divider()
                st.markdown("##### 🖼️ 现有照片")
                all_photos = get_all_photos()

                if all_photos.empty:
                    st.info("还没有任何物资照片。可以在上方上传。")
                else:
                    items_with_photos = [(row["物品名称"], row["照片"]) for _, row in all_photos.iterrows()]
                    render_photo_wall(items_with_photos, key_prefix="photo_tab_wall")
                    st.caption(f"共 {len(all_photos)} 个物资有照片。")

                    st.markdown("##### 🗑️ 删除照片")
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        del_photo_item = st.selectbox("选择要删除照片的物资", all_photos["物品名称"].tolist(), key="photo_tab_del_item")
                    with col2:
                        st.write("")
                        st.write("")
                        if st.button("🗑️ 删除该照片", key="photo_tab_btn_delete"):
                            delete_item_photo(del_photo_item)
                            st.success(f"已删除 {del_photo_item} 的照片")
                            st.rerun()


# ==================== Tab：按学期查询 ====================
if "📅 按学期查询" in tab_dict:
    with tab_dict["📅 按学期查询"]:
        st.subheader("按学期查询")
        semesters_df = get_semesters()
        if semesters_df.empty:
            st.warning("还没有学期，请先到「学期管理」添加。")
        else:
            col1, col2, col3 = st.columns(3)
            with col1:
                default_sem = get_default_semester(semesters_df)
                sem_list = semesters_df["学期名称"].tolist()
                default_idx = sem_list.index(default_sem) if default_sem in sem_list else 0
                sem_name = st.selectbox("选择学期", sem_list, index=default_idx)
            with col2:
                keyword2 = st.text_input("物品名称关键词（可选）", "", key="kw_sem")
            with col3:
                category_filter2 = st.selectbox("类别筛选", ["全部"] + CATEGORIES, key="cat_sem")
            row = semesters_df[semesters_df["学期名称"] == sem_name].iloc[0]
            start_dt = f"{row['开始日期']} 00:00:00"
            end_dt = f"{row['结束日期']} 23:59:59"
            st.caption(f"📅 学期区间：{row['开始日期']} ～ {row['结束日期']}")
            if st.button("查询学期数据", type="primary", key="btn_sem"):
                summary_df = query_summary(keyword2, start_dt, end_dt, category_filter2)
                if not summary_df.empty:
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("物品种类数", len(summary_df))
                    c2.metric("学期入库总量", f"{summary_df['期间入库'].sum():.0f}")
                    c3.metric("学期出库总量", f"{summary_df['期间出库'].sum():.0f}")
                    c4.metric("期末库存总量", f"{summary_df['期末库存'].sum():.0f}")
                st.markdown("### 📊 学期库存汇总")
                st.dataframe(summary_df, use_container_width=True, hide_index=True)
                st.markdown("### 🏆 学期消耗排行（出库 Top 10）")
                st.dataframe(query_ranking(start_dt, end_dt, 10), use_container_width=True, hide_index=True)
                st.markdown("### 📋 学期出入库明细")
                st.dataframe(query_logs(keyword2, start_dt, end_dt, category_filter2), use_container_width=True, hide_index=True)
                if not summary_df.empty:
                    st.download_button("⬇️ 导出学期汇总 CSV", summary_df.to_csv(index=False).encode("utf-8-sig"), file_name=f"{sem_name}_库存汇总.csv", mime="text/csv")


# ==================== Tab：学期管理 ====================
if "⚙️ 学期管理" in tab_dict:
    with tab_dict["⚙️ 学期管理"]:
        st.subheader("学期管理")
        st.markdown("##### 现有学期")
        semesters_df = get_semesters()
        st.dataframe(semesters_df, use_container_width=True, hide_index=True)
        st.markdown("##### 添加学期")
        with st.form("sem_form", clear_on_submit=True):
            col1, col2, col3 = st.columns(3)
            with col1:
                new_name = st.text_input("学期名称 *", placeholder="如 2026-2027学年第一学期")
            with col2:
                new_start = st.date_input("开始日期 *")
            with col3:
                new_end = st.date_input("结束日期 *")
            if st.form_submit_button("添加", type="primary"):
                if not new_name.strip():
                    st.error("学期名称不能为空")
                elif new_start > new_end:
                    st.error("开始日期不能晚于结束日期")
                else:
                    ok, msg = add_semester(new_name.strip(), new_start.strftime("%Y-%m-%d"), new_end.strftime("%Y-%m-%d"))
                    if ok:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)
        st.markdown("##### 删除学期")
        if not semesters_df.empty:
            col1, col2 = st.columns([3, 1])
            with col1:
                del_name = st.selectbox("选择要删除的学期", semesters_df["学期名称"].tolist())
            with col2:
                st.write("")
                st.write("")
                if st.button("删除"):
                    sid = int(semesters_df[semesters_df["学期名称"] == del_name]["id"].iloc[0])
                    delete_semester(sid)
                    st.success(f"已删除：{del_name}")
                    st.rerun()


# ==================== Tab：预警设置 ====================
if "🔔 预警设置" in tab_dict:
    with tab_dict["🔔 预警设置"]:
        st.subheader("🔔 库存预警设置")
        st.caption("为物品设置最低库存量。低于该值时，库存表会标注「⚠️ 库存不足」。")
        st.markdown("##### 当前预警列表")
        alerts_df = get_alerts()
        if alerts_df.empty:
            st.info("还没有设置任何预警。可在下方添加。")
        else:
            st.dataframe(alerts_df, use_container_width=True, hide_index=True)
        st.markdown("##### 添加 / 修改预警")
        items = get_all_items()
        col1, col2 = st.columns(2)
        with col1:
            alert_mode = st.radio("选择物品方式", ["从已有物品选择", "手动输入物品名称"], horizontal=True)
        with col2:
            if alert_mode == "从已有物品选择" and items:
                alert_item = st.selectbox("选择物品", items)
            else:
                alert_item = st.text_input("物品名称")
        alert_qty = st.number_input("最低库存阈值", min_value=0, step=1, value=0, format="%d")
        if st.button("💾 保存预警", type="primary"):
            if not alert_item or not alert_item.strip():
                st.error("请先选择或输入物品名称")
            else:
                set_alert(alert_item.strip(), alert_qty)
                st.success(f"已设置：【{alert_item}】最低库存 {alert_qty}")
                st.rerun()
        st.markdown("##### 删除预警")
        if not alerts_df.empty:
            col1, col2 = st.columns([3, 1])
            with col1:
                del_alert = st.selectbox("选择要删除预警的物品", alerts_df["物品名称"].tolist())
            with col2:
                st.write("")
                st.write("")
                if st.button("删除预警"):
                    delete_alert(del_alert)
                    st.success(f"已删除预警：{del_alert}")
                    st.rerun()


# ==================== Tab：用户管理 ====================
if "👥 用户管理" in tab_dict:
    with tab_dict["👥 用户管理"]:
        st.subheader("👥 用户管理")

        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT is_permanent_admin FROM app_users WHERE email = %s", (st.session_state.user,))
        _op_row = cur.fetchone()
        cur.close()
        conn.close()
        i_am_permanent = _op_row[0] if _op_row else False

        st.markdown("##### 🔎 搜索用户")
        search_kw = st.text_input("按 姓名 / 学号 / 邮箱 搜索", "", key="user_search", placeholder="输入姓名、学号或邮箱的一部分即可")
        users_df = list_users(search_kw)
        st.markdown("##### 📋 用户列表")
        if users_df.empty:
            st.info("没有匹配的用户。")
        else:
            st.markdown(render_user_table(users_df), unsafe_allow_html=True)
            st.caption(f"共 {len(users_df)} 位用户")

        st.divider()
        st.markdown("##### ➕ 添加新用户")
        st.caption("新增用户默认是 Operator，初始密码统一为 123456。")
        with st.form("add_user_form", clear_on_submit=True):
            col1, col2 = st.columns(2)
            with col1:
                new_name = st.text_input("姓名 *")
                new_email = st.text_input("邮箱 *")
            with col2:
                new_student_id = st.text_input("学号 *")
            if st.form_submit_button("添加用户", type="primary"):
                errors = []
                if not new_name or not new_name.strip():
                    errors.append("姓名不能为空")
                if not new_student_id or not new_student_id.strip():
                    errors.append("学号不能为空")
                if not new_email or not new_email.strip():
                    errors.append("邮箱不能为空")
                if errors:
                    for e in errors:
                        st.error(e)
                else:
                    ok, msg = create_user(new_email.strip(), new_name.strip(), new_student_id.strip())
                    if ok:
                        st.success(f"已添加：{new_name}（{new_student_id}）- {new_email}。{msg}")
                        st.rerun()
                    else:
                        st.error(msg)

        if i_am_permanent:
            st.divider()
            st.markdown("##### 👤 设置普通 Admin")
            st.caption("普通 Admin 在整个系统中只能有一个。指定新的人选后，原来的普通 Admin 会自动降级为 Operator。")
            conn = get_conn()
            cur = conn.cursor()
            cur.execute("SELECT email, name FROM app_users WHERE role = 'admin' AND is_permanent_admin = FALSE LIMIT 1")
            current_normal_admin = cur.fetchone()
            cur.close()
            conn.close()
            if current_normal_admin:
                st.info(f"当前普通 Admin：{current_normal_admin[1] or '未填姓名'}（{current_normal_admin[0]}）")
            else:
                st.warning("当前系统还没有普通 Admin。")
            na_search = st.text_input("搜索目标用户（姓名 / 学号 / 邮箱）", "", key="na_search", placeholder="输入关键词筛选")
            candidates_na = list_users(na_search) if na_search.strip() else list_users("")
            candidates_na = candidates_na[(candidates_na["永久Admin"] == False)]
            if current_normal_admin:
                candidates_na = candidates_na[candidates_na["邮箱"] != current_normal_admin[0]]
            if candidates_na.empty:
                st.info("没有可选用户。")
            else:
                display_na = [f"{r['姓名'] or '未填'}（{r['学号'] or '未填'}）- {r['邮箱']}" for _, r in candidates_na.iterrows()]
                selected_na = st.selectbox("选择用户", display_na, key="na_target")
                target_email_na = candidates_na.iloc[display_na.index(selected_na)]["邮箱"]
                if st.button("👤 设为普通 Admin", type="primary"):
                    ok, msg = set_normal_admin(target_email_na)
                    if ok:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)

        if not i_am_permanent:
            st.divider()
            st.markdown("##### 🔁 转让普通 Admin 权限")
            st.caption("转让后您将降为 Operator，目标用户成为新的普通 Admin。")
            other_users_df = list_users("")
            other_users_df = other_users_df[other_users_df["角色"] != "admin"].copy()
            if other_users_df.empty:
                st.info("暂无其他用户可接收 Admin 权限。")
            else:
                transfer_search = st.text_input("搜索接收者（姓名 / 学号 / 邮箱）", "", key="transfer_search", placeholder="输入关键词筛选")
                filtered_transfer = other_users_df
                if transfer_search.strip():
                    kw = transfer_search.strip()
                    mask = (filtered_transfer["姓名"].astype(str).str.contains(kw, case=False, na=False) |
                            filtered_transfer["学号"].astype(str).str.contains(kw, case=False, na=False) |
                            filtered_transfer["邮箱"].astype(str).str.contains(kw, case=False, na=False))
                    filtered_transfer = filtered_transfer[mask]
                if filtered_transfer.empty:
                    st.warning("没有匹配的用户。")
                else:
                    display_list = [f"{r['姓名'] or '未填'}（{r['学号'] or '未填'}）- {r['邮箱']}" for _, r in filtered_transfer.iterrows()]
                    selected_display = st.selectbox("选择接收者", display_list, key="transfer_target")
                    transfer_email = filtered_transfer.iloc[display_list.index(selected_display)]["邮箱"]
                    if st.button("确认转让", type="primary"):
                        ok, msg = transfer_admin(st.session_state.user, transfer_email)
                        if ok:
                            st.success(msg)
                            st.session_state.role = "operator"
                            st.rerun()
                        else:
                            st.error(msg)

        st.divider()
        st.markdown("##### 🔧 重置密码 / 删除用户")
        if users_df.empty:
            st.info("请先在搜索框里找到目标用户。")
        else:
            display_list2 = [f"{r['姓名'] or '未填'}（{r['学号'] or '未填'}）- {r['邮箱']}" for _, r in users_df.iterrows()]
            selected_user_display = st.selectbox("选择用户", display_list2, key="target_user")
            target_email = users_df.iloc[display_list2.index(selected_user_display)]["邮箱"]
            target_role = users_df.iloc[display_list2.index(selected_user_display)]["角色"]
            target_is_perm = users_df.iloc[display_list2.index(selected_user_display)]["永久Admin"]
            col1, col2 = st.columns(2)
            with col1:
                if st.button("重置密码为 123456"):
                    reset_user_password(target_email)
                    st.success(f"已重置 {target_email} 的密码为 123456。")
            with col2:
                if st.button("🗑️ 删除该用户"):
                    if target_email == st.session_state.user:
                        st.error("不能删除自己")
                    elif target_is_perm:
                        st.error("不能删除永久 Admin")
                    elif target_role == "admin" and not i_am_permanent:
                        st.error("不能删除管理员账号，请先转让 Admin 权限")
                    else:
                        delete_user(target_email)
                        st.success(f"已删除：{target_email}")
                        st.rerun()


# ==================== Tab：修改密码 ====================
if "📷 物资照片" in tab_dict:
    with tab_dict["📷 物资照片"]:
        # ===== 详情模式 =====
        if st.session_state.get("selected_photo_item"):
            item_name = st.session_state["selected_photo_item"]

            if st.button("← 返回照片墙", key="back_to_wall"):
                st.session_state["selected_photo_item"] = None
                st.rerun()

            st.markdown(f"### 📷 {item_name}")

            photo_b64 = get_item_photo(item_name)
            if not photo_b64:
                st.warning("该物资暂无照片。")

            col_left, col_right = st.columns([1, 1])

            with col_left:
                if photo_b64:
                    try:
                        img_bytes = base64.b64decode(photo_b64)
                        st.image(img_bytes, use_container_width=True)
                    except Exception:
                        st.warning("照片加载失败")
                    st.caption("提示：可右键图片 → 在新标签页打开图片查看原图。")

            with col_right:
                stock_qty = get_stock(item_name)
                st.metric(label="📦 当前库存", value=int(stock_qty) if stock_qty is not None else 0)

                st.markdown("##### 📋 最近 3 条出入库记录")
                recent_df = get_recent_logs(item_name, limit=3)
                if recent_df.empty:
                    st.info("该物资暂无出入库记录。")
                else:
                    st.dataframe(recent_df, use_container_width=True, hide_index=True)

        # ===== 照片墙模式 =====
        else:
            st.subheader("📷 物资照片管理")
            st.caption("点击「🔍 查看详情」可查看放大照片、当前库存和最近 3 条出入库记录。")

            existing_items = get_all_items()

            if not existing_items:
                st.info("暂无物资记录。请先到「📝 录入出入库」录入物资。")
            else:
                st.markdown("##### ⬆️ 上传 / 更新照片")
                col1, col2 = st.columns([1, 1])
                with col1:
                    photo_item = st.selectbox("选择物资", existing_items, key="photo_item_select")
                with col2:
                    photo_file = st.file_uploader(
                        "📷 选择照片（手机可直接拍照或从相册选择）",
                        type=["jpg", "jpeg", "png", "webp"],
                        key="photo_upload_file"
                    )

                if photo_file is not None:
                    st.image(photo_file, caption="照片预览", width=240)
                    if st.button("💾 保存照片", type="primary", key="btn_save_photo"):
                        b64 = compress_image(photo_file.getvalue())
                        if b64:
                            upsert_item_photo(photo_item, b64)
                            st.success(f"✅ 已保存 {photo_item} 的照片")
                            st.rerun()
                        else:
                            st.error("照片处理失败，请重试或换一张图片。")

                st.divider()
                st.markdown("##### 🖼️ 现有照片")
                all_photos = get_all_photos()

                if all_photos.empty:
                    st.info("还没有任何物资照片。可以在上方上传。")
                else:
                    items_with_photos = [(row["物品名称"], row["照片"]) for _, row in all_photos.iterrows()]
                    render_photo_wall(items_with_photos, key_prefix="wall_photo")
                    st.caption(f"共 {len(all_photos)} 个物资有照片。")

                    st.markdown("##### 🗑️ 删除照片")
                    col1, col2 = st.columns([3, 1])
                    with col1:
                        del_photo_item = st.selectbox("选择要删除照片的物资", all_photos["物品名称"].tolist(), key="del_photo_item")
                    with col2:
                        st.write("")
                        st.write("")
                        if st.button("🗑️ 删除该照片", key="btn_delete_photo"):
                            delete_item_photo(del_photo_item)
                            st.success(f"已删除 {del_photo_item} 的照片")
                            st.rerun()
