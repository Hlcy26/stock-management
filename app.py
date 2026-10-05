# -*- coding: utf-8 -*-
"""
团委学生会物资管理系统（Streamlit Cloud + Supabase）
含：永久 Admin 权限、密码修改2次限制、双角色体系
"""

import io
import os
from datetime import datetime, date, time

import pandas as pd
import psycopg2
import streamlit as st
from passlib.hash import bcrypt

st.set_page_config(page_title="团委学生会物资管理", layout="wide")

CATEGORIES = ["办公用品", "活动物资", "宣传用品", "奖品/证书", "借用物资", "其他"]
TAB_ROLES = {
    "📝 录入出入库": ["operator", "admin"],
    "🔍 库存与记录查询": ["operator", "admin"],
    "📅 按学期查询": ["operator", "admin"],
    "⚙️ 学期管理": ["admin"],
    "🔔 预警设置": ["operator", "admin"],
    "📤 导出数据": ["operator", "admin"],
    "👥 用户管理": ["admin"],
    "🔑 修改密码": ["operator", "admin"],
}

# ==================== 数据库连接 ====================
def get_conn():
    return psycopg2.connect(
        host=st.secrets["DB_HOST"], port=st.secrets["DB_PORT"],
        dbname=st.secrets["DB_NAME"], user=st.secrets["DB_USER"],
        password=st.secrets["DB_PASSWORD"], connect_timeout=10
    )

# ==================== 初始化 ====================
def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stock_log (
            id SERIAL PRIMARY KEY, item_name TEXT NOT NULL, category TEXT DEFAULT '其他',
            change_type TEXT NOT NULL CHECK(change_type IN ('IN','OUT')),
            quantity REAL NOT NULL CHECK(quantity > 0), log_time TEXT NOT NULL,
            note TEXT, operator TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
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
            "INSERT INTO app_users (email, password_hash, role, password_change_count, is_permanent_admin) "
            "VALUES (%s,%s,%s,%s,%s)",
            (default_email, bcrypt.hash(default_password), "admin", 0, True)
        )

    # 角色规范化：editor/viewer -> operator
    cur.execute("UPDATE app_users SET role = 'operator' WHERE role IN ('editor', 'viewer')")
    # 保证"普通 Admin"只有一个（永久 Admin 不算在内）
    cur.execute("SELECT email FROM app_users WHERE role = 'admin' AND is_permanent_admin = FALSE ORDER BY created_at")
    normal_admins = cur.fetchall()
    if len(normal_admins) > 1:
        for (email,) in normal_admins[1:]:
            cur.execute("UPDATE app_users SET role = 'operator' WHERE email = %s", (email,))
    conn.commit()
    cur.close()
    conn.close()

try:
    init_db()
except Exception as e:
    st.error(f"数据库初始化失败：{e}")
    st.stop()

# ==================== 用户管理 ====================
def get_user(identifier):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT email, password_hash, role, name, student_id, is_permanent_admin "
        "FROM app_users WHERE email = %s OR student_id = %s",
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
            "INSERT INTO app_users (email, name, student_id, password_hash, role, password_change_count, is_permanent_admin) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
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
    """普通 Admin 转让自己的权限给目标用户（转让后自己降为 Operator）"""
    if from_email == to_email:
        return False, "不能转让给自己"
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT role, is_permanent_admin FROM app_users WHERE email = %s", (from_email,))
        from_row = cur.fetchone()
        if not from_row:
            return False, "系统错误：找不到您的账号"
        if from_row[1]:  # is_permanent_admin
            return False, "您是永久 Admin，不能通过此方式转让。如需变更，请使用「设置永久 Admin」功能。"

        cur.execute("SELECT role, is_permanent_admin FROM app_users WHERE email = %s", (to_email,))
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

def set_permanent_admin(target_email, is_permanent):
    """设置/取消某用户的永久 Admin（仅永久 Admin 可操作）"""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT is_permanent_admin FROM app_users WHERE email = %s", (st.session_state.user,))
        op_row = cur.fetchone()
        if not op_row or not op_row[0]:
            return False, "只有永久 Admin 可以执行此操作"

        cur.execute("SELECT role FROM app_users WHERE email = %s", (target_email,))
        row = cur.fetchone()
        if not row:
            return False, "目标用户不存在"

        if is_permanent:
            cur.execute("UPDATE app_users SET role = 'admin', is_permanent_admin = TRUE WHERE email = %s", (target_email,))
            msg = f"✅ 已将 {target_email} 设为永久 Admin"
        else:
            cur.execute("UPDATE app_users SET is_permanent_admin = FALSE WHERE email = %s", (target_email,))
            msg = f"✅ 已取消 {target_email} 的永久 Admin 标记"
        conn.commit()
        return True, msg
    except Exception as e:
        conn.rollback()
        return False, str(e)
    finally:
        cur.close()
        conn.close()

def reset_user_password(email):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE app_users SET password_hash = %s, password_change_count = 1 WHERE email = %s",
        (bcrypt.hash("123456"), email)
    )
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
    sql = ("SELECT email AS 邮箱, name AS 姓名, student_id AS 学号, role AS 角色, "
           "is_permanent_admin AS 永久Admin, password_change_count AS 已修改次数, "
           "created_at AS 创建时间 FROM app_users")
    params = []
    if keyword and keyword.strip():
        sql += " WHERE name ILIKE %s OR student_id ILIKE %s OR email ILIKE %s "
        kw = f"%{keyword.strip()}%"
        params = [kw, kw, kw]
    sql += " ORDER BY created_at"
    df = pd.read_sql_query(sql, conn, params=params if params else None)
    conn.close()
    return df

def change_user_password(current_login_email, input_email, input_name, input_student_id, new_password):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT email, name, student_id, password_change_count FROM app_users WHERE email = %s", (current_login_email,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close(); return False, "系统错误：用户不存在"
    db_email, db_name, db_student_id, change_count = row

    if input_email.strip() != db_email:
        cur.close(); conn.close(); return False, "❌ 输入的邮箱与当前登录账号不匹配"
    if input_name.strip() != db_name:
        cur.close(); conn.close(); return False, "❌ 输入的姓名与系统记录不匹配"
    if input_student_id.strip() != db_student_id:
        cur.close(); conn.close(); return False, "❌ 输入的学号与系统记录不匹配"

    if change_count >= 2:
        cur.close(); conn.close(); return False, "您已经修改过 2 次密码，不能再自行修改。请联系管理员重置。"

    cur.execute("UPDATE app_users SET password_hash = %s, password_change_count = password_change_count + 1 WHERE email = %s",
                (bcrypt.hash(new_password), current_login_email))
    conn.commit()
    cur.close(); conn.close()
    return True, f"✅ 密码修改成功（这是您第 {change_count + 1} 次修改）"

# ==================== 出入库 ====================
def insert_log(item_name, category, change_type, quantity, log_time, note, operator):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO stock_log(item_name, category, change_type, quantity, log_time, note, operator) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (item_name, category, change_type, quantity, log_time, note, operator))
    conn.commit(); cur.close(); conn.close()

def get_stock(item_name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(SUM(CASE WHEN change_type='IN' THEN quantity ELSE -quantity END), 0) FROM stock_log WHERE item_name = %s", (item_name,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row[0] if row else 0

def _parse_datetime(row):
    d = row.get("日期"); t = row.get("时间", "00:00:00")
    if d is None or pd.isna(d): raise ValueError("日期不能为空")
    d_str = pd.to_datetime(d).strftime("%Y-%m-%d")
    if t is None or pd.isna(t) or str(t).strip() == "": t_str = "00:00:00"
    else:
        t_str = str(t).strip(); parts = t_str.split(":")
        if len(parts) == 2: t_str = f"{parts[0].zfill(2)}:{parts[1].zfill(2)}:00"
        elif len(parts) == 3: t_str = f"{parts[0].zfill(2)}:{parts[1].zfill(2)}:{parts[2].zfill(2)}"
        else: t_str = "00:00:00"
    return f"{d_str} {t_str}"

def import_logs_from_excel(df, default_operator):
    success = 0; errors = []
    conn = get_conn(); cur = conn.cursor()
    for idx, row in df.iterrows():
        row_num = idx + 2
        try:
            item = str(row.get("物品名称", "")).strip()
            if not item: errors.append(f"第 {row_num} 行：物品名称为空"); continue
            category = str(row.get("类别", "")).strip() or "其他"
            if category not in CATEGORIES: category = "其他"
            ctype_cn = str(row.get("类型", "")).strip()
            if ctype_cn not in ("入库", "出库"): errors.append(f"第 {row_num} 行：类型必须是「入库」或「出库」"); continue
            try: qty = float(row.get("数量"))
            except (TypeError, ValueError): errors.append(f"第 {row_num} 行：数量不是有效数字"); continue
            if qty <= 0: errors.append(f"第 {row_num} 行：数量必须大于 0"); continue
            try: log_time = _parse_datetime(row)
            except Exception as e: errors.append(f"第 {row_num} 行：{e}"); continue
            operator_val = str(row.get("操作人", "")).strip() or default_operator
            note_val = row.get("备注", "")
            note_val = "" if (note_val is None or pd.isna(note_val)) else str(note_val).strip()
            change_type = "IN" if ctype_cn == "入库" else "OUT"
            if change_type == "OUT":
                cur.execute("SELECT COALESCE(SUM(CASE WHEN change_type='IN' THEN quantity ELSE -quantity END), 0) FROM stock_log WHERE item_name = %s", (item,))
                stock = cur.fetchone()[0]
                if stock < qty: errors.append(f"第 {row_num} 行：{item} 库存不足（当前 {stock}，需出库 {qty}）"); continue
            cur.execute("INSERT INTO stock_log(item_name, category, change_type, quantity, log_time, note, operator) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (item, category, change_type, qty, log_time, note_val, operator_val))
            success += 1
        except Exception as e: errors.append(f"第 {row_num} 行：{e}")
    conn.commit(); cur.close(); conn.close()
    return success, errors

# ==================== 查询 ====================
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
        sql += " AND sl.category = %s"; params.append(category)
    sql += " GROUP BY sl.item_name ORDER BY sl.item_name"
    df = pd.read_sql_query(sql, conn, params=params)
    conn.close()
    if not df.empty:
        df["状态"] = df.apply(lambda r: "⚠️ 库存不足" if pd.notna(r["预警阈值"]) and r["当前库存"] < r["预警阈值"] else "✅ 正常", axis=1)
    return df

def query_stock_only(keyword="", category="全部"):
    df = query_stock(keyword, category)
    if df.empty: return df
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
        sql += " AND category = %s"; params.append(category)
    sql += " ORDER BY log_time DESC"
    df = pd.read_sql_query(sql, conn, params=params)
    conn.close(); return df

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
        sql += " AND category = %s"; params.append(category)
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
    conn.close(); return df

def get_all_items():
    conn = get_conn()
    df = pd.read_sql_query("SELECT DISTINCT item_name FROM stock_log ORDER BY item_name", conn)
    conn.close(); return df["item_name"].tolist()

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
    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT INTO stock_alert(item_name, min_quantity, updated_at) VALUES (%s, %s, CURRENT_TIMESTAMP) ON CONFLICT(item_name) DO UPDATE SET min_quantity = EXCLUDED.min_quantity, updated_at = CURRENT_TIMESTAMP", (item_name, min_quantity))
    conn.commit(); cur.close(); conn.close()

def delete_alert(item_name):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM stock_alert WHERE item_name=%s", (item_name,))
    conn.commit(); cur.close(); conn.close()

# ==================== 学期 ====================
def get_semesters():
    conn = get_conn()
    df = pd.read_sql_query("SELECT id, name AS 学期名称, start_date AS 开始日期, end_date AS 结束日期 FROM semesters ORDER BY start_date DESC", conn)
    conn.close(); return df

def add_semester(name, start_date, end_date):
    conn = get_conn(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO semesters(name, start_date, end_date) VALUES (%s,%s,%s)", (name, start_date, end_date))
        conn.commit(); return True, "添加成功"
    except psycopg2.errors.UniqueViolation:
        conn.rollback(); return False, "学期名称已存在"
    finally:
        cur.close(); conn.close()

def delete_semester(sid):
    conn = get_conn(); cur = conn.cursor()
    cur.execute("DELETE FROM semesters WHERE id=%s", (sid,))
    conn.commit(); cur.close(); conn.close()

def get_default_semester(semesters_df):
    today = date.today().strftime("%Y-%m-%d")
    for _, row in semesters_df.iterrows():
        if row["开始日期"] <= today <= row["结束日期"]: return row["学期名称"]
    return semesters_df.iloc[0]["学期名称"] if not semesters_df.empty else None

# ==================== 导出 ====================
def _prepare_stock_df(keyword="", category="全部"):
    df = query_stock(keyword, category)
    if df.empty: return df
    return df[["物品名称", "类别", "累计入库", "累计出库", "当前库存", "预警阈值", "状态"]]

def export_to_excel(keyword="", category="全部", include_logs=False, log_start=None, log_end=None):
    stock_df = _prepare_stock_df(keyword, category)
    if stock_df.empty: return None
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        stock_df.to_excel(writer, sheet_name="库存汇总", index=False)
        if include_logs and log_start and log_end:
            logs_df = query_logs(keyword, log_start, log_end, category)
            if not logs_df.empty:
                logs_df.to_excel(writer, sheet_name="出入库记录", index=False)
    return output.getvalue()

def generate_import_template():
    today = date.today().strftime("%Y-%m-%d")
    template_df = pd.DataFrame({
        "物品名称": ["示例：中性笔", "示例：A4纸"], "类别": ["办公用品", "办公用品"],
        "类型": ["入库", "出库"], "数量": [20, 5], "日期": [today, today],
        "时间": ["10:00:00", "14:30:00"], "操作人": ["张三", "李四"], "备注": ["示例行，可删除", "示例行，可删除"],
    })
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        template_df.to_excel(writer, sheet_name="出入库导入", index=False)
    return output.getvalue()

def _register_chinese_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    candidates = [("SimHei", "C:/Windows/Fonts/simhei.ttf"), ("MicrosoftYaHei", "C:/Windows/Fonts/msyh.ttf"),
                  ("SimSun", "C:/Windows/Fonts/simsun.ttf"), ("PingFang", "/System/Library/Fonts/PingFang.ttc"),
                  ("WQY", "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc")]
    for name, path in candidates:
        if os.path.exists(path):
            try: pdfmetrics.registerFont(TTFont(name, path)); return name
            except Exception: continue
    return "Helvetica"

def export_to_pdf(keyword="", category="全部", include_logs=False, log_start=None, log_end=None):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    stock_df = _prepare_stock_df(keyword, category)
    if stock_df.empty: return None
    font_name = _register_chinese_font()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), leftMargin=12*mm, rightMargin=12*mm, topMargin=12*mm, bottomMargin=12*mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("T", parent=styles["Title"], fontName=font_name, fontSize=18, leading=24)
    h_style = ParagraphStyle("H", parent=styles["Heading2"], fontName=font_name, fontSize=13, leading=18)
    normal_style = ParagraphStyle("N", parent=styles["Normal"], fontName=font_name, fontSize=10, leading=14)
    elements = [Paragraph("团委学生会物资库存报表", title_style), Spacer(1, 4*mm),
                Paragraph(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", normal_style), Spacer(1, 6*mm)]
    def df_to_table(df, font_size=9):
        data = [list(df.columns)] + df.fillna("").astype(str).values.tolist()
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), font_name), ("FONTSIZE", (0, 0), (-1, -1), font_size),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4A6FA5")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F5FA")])]))
        return t
    elements.append(Paragraph("一、库存汇总", h_style)); elements.append(Spacer(1, 2*mm))
    elements.append(df_to_table(stock_df, font_size=9))
    if include_logs and log_start and log_end:
        logs_df = query_logs(keyword, log_start, log_end, category)
        if not logs_df.empty:
            elements.append(Spacer(1, 8*mm)); elements.append(Paragraph("二、出入库记录", h_style))
            elements.append(Spacer(1, 2*mm)); elements.append(df_to_table(logs_df, font_size=8))
    doc.build(elements); return buffer.getvalue()

# ==================== 登录 ====================
if "user" not in st.session_state:
    st.session_state.user = None; st.session_state.role = None
    st.session_state.name = None; st.session_state.student_id = None

if st.session_state.user is None:
    st.title("📦 团委学生会物资管理系统"); st.subheader("请登录")
    with st.form("login_form"):
        identifier = st.text_input("邮箱或学号"); password = st.text_input("密码", type="password")
        if st.form_submit_button("登录", type="primary"):
            if not identifier or not password: st.error("请填写账号和密码")
            else:
                user = get_user(identifier.strip())
                if user and bcrypt.verify(password, user[1]):
                    st.session_state.user = user[0]; st.session_state.role = user[2]
                    st.session_state.name = user[3]; st.session_state.student_id = user[4]
                    st.rerun()
                else: st.error("账号或密码错误")
    st.stop()

if not st.session_state.name or not st.session_state.student_id:
    st.title("📦 团委学生会物资管理系统"); st.subheader("首次登录，请完善个人信息")
    with st.form("complete_profile"):
        col1, col2 = st.columns(2)
        with col1: name = st.text_input("姓名 *")
        with col2: student_id = st.text_input("学号 *")
        if st.form_submit_button("确认", type="primary"):
            if not name.strip(): st.error("姓名不能为空")
            elif not student_id.strip(): st.error("学号不能为空")
            else:
                update_user_profile(st.session_state.user, name.strip(), student_id.strip())
                st.session_state.name = name.strip(); st.session_state.student_id = student_id.strip(); st.rerun()
    st.stop()

# ==================== 主应用 ====================
st.title("📦 团委学生会物资管理系统")
st.sidebar.markdown(f"**当前用户**：{st.session_state.name}")
st.sidebar.markdown(f"**学号**：{st.session_state.student_id}")
st.sidebar.markdown(f"**邮箱**：{st.session_state.user}")
_role_label = "Admin（管理员）" if st.session_state.role == "admin" else "Operator（操作员）"
st.sidebar.markdown(f"**角色**：{_role_label}")
if st.sidebar.button("退出登录"):
    st.session_state.user = None; st.session_state.role = None
    st.session_state.name = None; st.session_state.student_id = None; st.rerun()

_alerts = get_alerts()
if not _alerts.empty:
    _short = _alerts[_alerts["状态"] == "⚠️ 库存不足"]
    if not _short.empty:
        st.warning(f"⚠️ 当前有 {len(_short)} 种物品库存低于预警阈值，请及时补充。")

role = st.session_state.role
allowed = [name for name, roles in TAB_ROLES.items() if role in roles]
tab_objs = st.tabs(allowed)
tab_dict = dict(zip(allowed, tab_objs))

# ---------- 录入出入库 ----------
if "📝 录入出入库" in tab_dict:
    with tab_dict["📝 录入出入库"]:
        st.subheader("录入出入库")
        mode = st.radio("录入方式", ["✍️ 单条录入", "📥 批量导入 Excel"], horizontal=True, key="entry_mode")
        if mode == "✍️ 单条录入":
            existing_items = get_all_items()
            with st.form("entry_form", clear_on_submit=True):
                col1, col2, col3 = st.columns(3)
                with col1:
                    if existing_items:
                        item_name = st.selectbox("物品名称 *", options=existing_items, index=None, accept_new_options=True, placeholder="输入首字即可搜索，或输入新物品", key="item_select")
                    else:
                        item_name = st.text_input("物品名称 *（暂无历史物品）")
                    category = st.selectbox("类别 *", CATEGORIES)
                with col2:
                    change_type_cn = st.selectbox("类型 *", ["入库", "出库"])
                    quantity = st.number_input("数量 *", min_value=0.01, step=1.0, format="%.2f")
                with col3:
                    log_date = st.date_input("日期 *", value=date.today())
                    log_time_part = st.time_input("时间 *", value=datetime.now().time().replace(second=0, microsecond=0))
                col4, col5 = st.columns(2)
                with col4: operator = st.text_input("操作人 *", value=st.session_state.name or "")
                with col5: note = st.text_input("备注")
                if st.form_submit_button("提交", type="primary"):
                    errors = []
                    if not item_name or not str(item_name).strip(): errors.append("物品名称不能为空")
                    if not operator or not operator.strip(): errors.append("操作人不能为空")
                    if errors:
                        for e in errors: st.error(e)
                    else:
                        log_time = datetime.combine(log_date, log_time_part).strftime("%Y-%m-%d %H:%M:%S")
                        change_type = "IN" if change_type_cn == "入库" else "OUT"
                        clean_name = str(item_name).strip(); clean_operator = operator.strip()
                        if change_type == "OUT":
                            stock = get_stock(clean_name)
                            if stock < quantity: st.error(f"库存不足！【{clean_name}】当前库存：{stock}")
                            else:
                                insert_log(clean_name, category, change_type, quantity, log_time, note, clean_operator)
                                st.success(f"✅ 已录入出库：{clean_name} × {quantity}")
                        else:
                            insert_log(clean_name, category, change_type, quantity, log_time, note, clean_operator)
                            st.success(f"✅ 已录入入库：{clean_name} × {quantity}")
        else:
            st.markdown("##### 📋 使用说明"); st.markdown("1. 下载模板 2. 填写 3. 上传 4. 确认导入")
            col1, col2 = st.columns(2)
            with col1: st.download_button("⬇️ 下载 Excel 模板", generate_import_template(), file_name="出入库导入模板.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            uploaded_file = st.file_uploader("上传填好的 Excel 文件（.xlsx）", type=["xlsx"], key="import_file")
            if uploaded_file is not None:
                try:
                    df_import = pd.read_excel(uploaded_file, sheet_name=0)
                    df_import.columns = [str(c).strip() for c in df_import.columns]
                    required_cols = {"物品名称", "类型", "数量", "日期"}
                    missing = required_cols - set(df_import.columns)
                    if missing: st.error(f"缺少必需的列：{', '.join(missing)}。请下载模板并按格式填写。")
                    elif df_import.empty: st.warning("文件中没有数据行。")
                    else:
                        st.markdown("##### 📄 数据预览"); st.dataframe(df_import, use_container_width=True, hide_index=True)
                        st.caption(f"共 {len(df_import)} 行待导入")
                        default_op = st.text_input("默认操作人（当行内未填操作人时使用）", value=st.session_state.name or st.session_state.user, key="import_default_operator")
                        if st.button("✅ 确认导入", type="primary", key="btn_import"):
                            with st.spinner("导入中..."):
                                success, errors = import_logs_from_excel(df_import, default_op.strip())
                            if success > 0: st.success(f"✅ 成功导入 {success} 条记录")
                            if errors:
                                with st.expander(f"⚠️ 有 {len(errors)} 行未导入，点击查看原因"):
                                    for e in errors: st.write(f"- {e}")
                            if success > 0 and not errors: st.balloons()
                except Exception as e: st.error(f"读取文件失败：{e}")

# ---------- 库存与记录查询 ----------
if "🔍 库存与记录查询" in tab_dict:
    with tab_dict["🔍 库存与记录查询"]:
        st.subheader("库存与出入库记录查询")
        col1, col2, col3, col4 = st.columns(4)
        with col1: keyword = st.text_input("物品名称关键词", "")
        with col2: category_filter = st.selectbox("类别筛选", ["全部"] + CATEGORIES)
        with col3: start_date = st.date_input("开始日期", value=date.today().replace(day=1))
        with col4: end_date = st.date_input("结束日期", value=date.today())
        only_stock = st.checkbox("🔎 只看库存（不显示时间段汇总和明细）", value=False)
        if st.button("查询", type="primary", key="btn_query"):
            start_dt = datetime.combine(start_date, time.min).strftime("%Y-%m-%d %H:%M:%S")
            end_dt = datetime.combine(end_date, time.max).strftime("%Y-%m-%d %H:%M:%S")
            if only_stock:
                st.markdown("### 📊 所有物品库存"); st.dataframe(query_stock_only(keyword, category_filter), use_container_width=True, hide_index=True)
            else:
                st.markdown("### 📊 当前库存"); st.dataframe(query_stock(keyword, category_filter), use_container_width=True, hide_index=True)
                st.markdown("### 📈 期间汇总"); st.dataframe(query_summary(keyword, start_dt, end_dt, category_filter), use_container_width=True, hide_index=True)
                st.markdown("### 📋 出入库明细"); logs_df = query_logs(keyword, start_dt, end_dt, category_filter)
                st.dataframe(logs_df, use_container_width=True, hide_index=True)
                if not logs_df.empty: st.download_button("⬇️ 导出明细 CSV", logs_df.to_csv(index=False).encode("utf-8-sig"), file_name=f"出入库明细_{start_date}_{end_date}.csv", mime="text/csv")

# ---------- 按学期查询 ----------
if "📅 按学期查询" in tab_dict:
    with tab_dict["📅 按学期查询"]:
        st.subheader("按学期查询")
        semesters_df = get_semesters()
        if semesters_df.empty: st.warning("还没有学期，请先到「学期管理」添加。")
        else:
            col1, col2, col3 = st.columns(3)
            with col1:
                default_sem = get_default_semester(semesters_df); sem_list = semesters_df["学期名称"].tolist()
                default_idx = sem_list.index(default_sem) if default_sem in sem_list else 0
                sem_name = st.selectbox("选择学期", sem_list, index=default_idx)
            with col2: keyword2 = st.text_input("物品名称关键词（可选）", "", key="kw_sem")
            with col3: category_filter2 = st.selectbox("类别筛选", ["全部"] + CATEGORIES, key="cat_sem")
            row = semesters_df[semesters_df["学期名称"] == sem_name].iloc[0]
            start_dt = f"{row['开始日期']} 00:00:00"; end_dt = f"{row['结束日期']} 23:59:59"
            st.caption(f"📅 学期区间：{row['开始日期']} ～ {row['结束日期']}")
            if st.button("查询学期数据", type="primary", key="btn_sem"):
                summary_df = query_summary(keyword2, start_dt, end_dt, category_filter2)
                if not summary_df.empty:
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("物品种类数", len(summary_df)); c2.metric("学期入库总量", f"{summary_df['期间入库'].sum():.0f}")
                    c3.metric("学期出库总量", f"{summary_df['期间出库'].sum():.0f}"); c4.metric("期末库存总量", f"{summary_df['期末库存'].sum():.0f}")
                st.markdown("### 📊 学期库存汇总"); st.dataframe(summary_df, use_container_width=True, hide_index=True)
                st.markdown("### 🏆 学期消耗排行（出库 Top 10）"); st.dataframe(query_ranking(start_dt, end_dt, 10), use_container_width=True, hide_index=True)
                st.markdown("### 📋 学期出入库明细"); st.dataframe(query_logs(keyword2, start_dt, end_dt, category_filter2), use_container_width=True, hide_index=True)
                if not summary_df.empty: st.download_button("⬇️ 导出学期汇总 CSV", summary_df.to_csv(index=False).encode("utf-8-sig"), file_name=f"{sem_name}_库存汇总.csv", mime="text/csv")

# ---------- 学期管理 ----------
if "⚙️ 学期管理" in tab_dict:
    with tab_dict["⚙️ 学期管理"]:
        st.subheader("学期管理"); st.markdown("##### 现有学期"); semesters_df = get_semesters()
        st.dataframe(semesters_df, use_container_width=True, hide_index=True)
        st.markdown("##### 添加学期")
        with st.form("sem_form", clear_on_submit=True):
            col1, col2, col3 = st.columns(3)
            with col1: new_name = st.text_input("学期名称 *", placeholder="如 2026-2027学年第一学期")
            with col2: new_start = st.date_input("开始日期 *")
            with col3: new_end = st.date_input("结束日期 *")
            if st.form_submit_button("添加", type="primary"):
                if not new_name.strip(): st.error("学期名称不能为空")
                elif new_start > new_end: st.error("开始日期不能晚于结束日期")
                else:
                    ok, msg = add_semester(new_name.strip(), new_start.strftime("%Y-%m-%d"), new_end.strftime("%Y-%m-%d"))
                    if ok: st.success(msg); st.rerun()
                    else: st.error(msg)
        st.markdown("##### 删除学期")
        if not semesters_df.empty:
            col1, col2 = st.columns([3, 1])
            with col1: del_name = st.selectbox("选择要删除的学期", semesters_df["学期名称"].tolist())
            with col2:
                st.write(""); st.write("")
                if st.button("删除"):
                    sid = int(semesters_df[semesters_df["学期名称"] == del_name]["id"].iloc[0]); delete_semester(sid)
                    st.success(f"已删除：{del_name}"); st.rerun()

# ---------- 预警设置 ----------
if "🔔 预警设置" in tab_dict:
    with tab_dict["🔔 预警设置"]:
        st.subheader("🔔 库存预警设置"); st.caption("为物品设置最低库存量。低于该值时，库存表会标注「⚠️ 库存不足」。")
        st.markdown("##### 当前预警列表"); alerts_df = get_alerts()
        if alerts_df.empty: st.info("还没有设置任何预警。可在下方添加。")
        else: st.dataframe(alerts_df, use_container_width=True, hide_index=True)
        st.markdown("##### 添加 / 修改预警"); items = get_all_items()
        col1, col2 = st.columns(2)
        with col1: alert_mode = st.radio("选择物品方式", ["从已有物品选择", "手动输入物品名称"], horizontal=True)
        with col2:
            if alert_mode == "从已有物品选择" and items: alert_item = st.selectbox("选择物品", items)
            else: alert_item = st.text_input("物品名称")
        alert_qty = st.number_input("最低库存阈值", min_value=0.0, step=1.0, format="%.2f")
        if st.button("💾 保存预警", type="primary"):
            if not alert_item or not alert_item.strip(): st.error("请先选择或输入物品名称")
            else: set_alert(alert_item.strip(), alert_qty); st.success(f"已设置：【{alert_item}】最低库存 {alert_qty}"); st.rerun()
        st.markdown("##### 删除预警")
        if not alerts_df.empty:
            col1, col2 = st.columns([3, 1])
            with col1: del_alert = st.selectbox("选择要删除预警的物品", alerts_df["物品名称"].tolist())
            with col2:
                st.write(""); st.write("")
                if st.button("删除预警"): delete_alert(del_alert); st.success(f"已删除预警：{del_alert}"); st.rerun()

# ---------- 导出数据 ----------
if "📤 导出数据" in tab_dict:
    with tab_dict["📤 导出数据"]:
        st.subheader("📤 一键导出"); st.caption("导出所有物品的库存数据；可选附带出入库记录。")
        col1, col2, col3 = st.columns(3)
        with col1: exp_keyword = st.text_input("物品名称关键词（可选）", "", key="exp_kw")
        with col2: exp_category = st.selectbox("类别筛选", ["全部"] + CATEGORIES, key="exp_cat")
        with col3: exp_format = st.selectbox("导出格式", ["Excel (.xlsx)", "PDF (.pdf)"])
        include_logs = st.checkbox("📋 同时导出出入库记录（需选择时间范围）", value=False)
        exp_start_date = exp_end_date = None
        if include_logs:
            col1, col2 = st.columns(2)
            with col1: exp_start_date = st.date_input("记录开始日期", value=date.today().replace(day=1), key="exp_start")
            with col2: exp_end_date = st.date_input("记录结束日期", value=date.today(), key="exp_end")
        st.divider(); preview_df = _prepare_stock_df(exp_keyword, exp_category); st.markdown("##### 库存数据预览")
        if preview_df.empty: st.info("没有可导出的数据。")
        else: st.dataframe(preview_df, use_container_width=True, hide_index=True); st.caption(f"共 {len(preview_df)} 种物品")
        if st.button("🚀 生成并下载", type="primary"):
            if preview_df.empty: st.error("当前没有可导出的库存数据。")
            else:
                log_start = log_end = None
                if include_logs and exp_start_date and exp_end_date:
                    log_start = datetime.combine(exp_start_date, time.min).strftime("%Y-%m-%d %H:%M:%S")
                    log_end = datetime.combine(exp_end_date, time.max).strftime("%Y-%m-%d %H:%M:%S")
                try:
                    if exp_format.startswith("Excel"):
                        data = export_to_excel(exp_keyword, exp_category, include_logs, log_start, log_end)
                        if data is None: st.error("没有可导出的数据。")
                        else: st.download_button("⬇️ 点击下载 Excel", data, file_name=f"物资库存_{date.today()}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary"); st.success("✅ Excel 已生成。")
                    else:
                        data = export_to_pdf(exp_keyword, exp_category, include_logs, log_start, log_end)
                        if data is None: st.error("没有可导出的数据。")
                        else: st.download_button("⬇️ 点击下载 PDF", data, file_name=f"物资库存_{date.today()}.pdf", mime="application/pdf", type="primary"); st.success("✅ PDF 已生成。")
                except Exception as e: st.error(f"导出失败：{e}")

# ---------- 用户管理 ----------
if "👥 用户管理" in tab_dict:
    with tab_dict["👥 用户管理"]:
        st.subheader("👥 用户管理")
        st.caption("系统有三种角色：**Operator**（录入、查询、导出）、**Admin**（普通管理员，唯一）、**永久 Admin**（可以多个，权限最高，不会被降级）。")

        # 检查当前用户是否为永久 admin
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT is_permanent_admin FROM app_users WHERE email = %s", (st.session_state.user,))
        _op_row = cur.fetchone()
        cur.close(); conn.close()
        i_am_permanent = _op_row[0] if _op_row else False

        st.markdown("##### 🔎 搜索用户")
        search_kw = st.text_input("按 姓名 / 学号 / 邮箱 搜索", "", key="user_search", placeholder="输入姓名、学号或邮箱的一部分即可")
        users_df = list_users(search_kw)
        st.markdown("##### 📋 用户列表")
        if users_df.empty: st.info("没有匹配的用户。")
        else: st.dataframe(users_df, use_container_width=True, hide_index=True); st.caption(f"共 {len(users_df)} 位用户")

        st.divider()
        st.markdown("##### ➕ 添加新用户")
        st.caption("新增用户默认是 Operator，初始密码统一为 123456。")
        with st.form("add_user_form", clear_on_submit=True):
            col1, col2 = st.columns(2)
            with col1: new_name = st.text_input("姓名 *"); new_email = st.text_input("邮箱 *")
            with col2: new_student_id = st.text_input("学号 *")
            if st.form_submit_button("添加用户", type="primary"):
                errors = []
                if not new_name or not new_name.strip(): errors.append("姓名不能为空")
                if not new_student_id or not new_student_id.strip(): errors.append("学号不能为空")
                if not new_email or not new_email.strip(): errors.append("邮箱不能为空")
                if errors:
                    for e in errors: st.error(e)
                else:
                    ok, msg = create_user(new_email.strip(), new_name.strip(), new_student_id.strip())
                    if ok: st.success(f"已添加：{new_name}（{new_student_id}）- {new_email}。{msg}"); st.rerun()
                    else: st.error(msg)

        # 永久 Admin 专属功能
        if i_am_permanent:
            st.divider()
            st.markdown("##### ⭐ 设置 / 取消永久 Admin")
            st.caption("永久 Admin 拥有全部权限，且不会被降级。你可以指定多个永久 Admin。")

            perm_search = st.text_input("搜索目标用户（姓名 / 学号 / 邮箱）", "", key="perm_search", placeholder="输入关键词筛选")
            candidates_df = list_users(perm_search) if perm_search.strip() else list_users("")
            # 排除自己
            candidates_df = candidates_df[candidates_df["邮箱"] != st.session_state.user]

            if candidates_df.empty:
                st.info("没有可选用户。")
            else:
                display_list = [f"{r['姓名'] or '未填'}（{r['学号'] or '未填'}）- {r['邮箱']}｜当前角色：{r['角色']}{'（永久）' if r['永久Admin'] else ''}" for _, r in candidates_df.iterrows()]
                selected_display = st.selectbox("选择用户", display_list, key="perm_target")
                target_row = candidates_df.iloc[display_list.index(selected_display)]
                target_email = target_row["邮箱"]
                is_perm_now = target_row["永久Admin"]

                col1, col2 = st.columns(2)
                with col1:
                    if not is_perm_now:
                        if st.button("⭐ 设为永久 Admin"):
                            ok, msg = set_permanent_admin(target_email, True)
                            if ok: st.success(msg); st.rerun()
                            else: st.error(msg)
                    else:
                        st.info("该用户已是永久 Admin")
                with col2:
                    if is_perm_now:
                        if st.button("🚫 取消永久 Admin 标记"):
                            ok, msg = set_permanent_admin(target_email, False)
                            if ok: st.success(msg); st.rerun()
                            else: st.error(msg)

        st.divider()
        st.markdown("##### 🔁 转让普通 Admin 权限")
        st.caption("此项仅对**普通 Admin**有效。转让后您将降为 Operator。永久 Admin 无需使用此功能。")
        if i_am_permanent:
            st.info("您是永久 Admin，无需转让权限。")
        else:
            other_users_df = list_users(""); other_users_df = other_users_df[other_users_df["角色"] != "admin"].copy()
            if other_users_df.empty: st.info("暂无其他用户可接收 Admin 权限。")
            else:
                transfer_search = st.text_input("搜索接收者（姓名 / 学号 / 邮箱）", "", key="transfer_search", placeholder="输入关键词筛选")
                filtered_transfer = other_users_df
                if transfer_search.strip():
                    kw = transfer_search.strip()
                    mask = (filtered_transfer["姓名"].astype(str).str.contains(kw, case=False, na=False) |
                            filtered_transfer["学号"].astype(str).str.contains(kw, case=False, na=False) |
                            filtered_transfer["邮箱"].astype(str).str.contains(kw, case=False, na=False))
                    filtered_transfer = filtered_transfer[mask]
                if filtered_transfer.empty: st.warning("没有匹配的用户。")
                else:
                    display_list = [f"{r['姓名'] or '未填'}（{r['学号'] or '未填'}）- {r['邮箱']}" for _, r in filtered_transfer.iterrows()]
                    selected_display = st.selectbox("选择接收者", display_list, key="transfer_target")
                    transfer_email = filtered_transfer.iloc[display_list.index(selected_display)]["邮箱"]
                    if st.button("确认转让", type="primary"):
                        ok, msg = transfer_admin(st.session_state.user, transfer_email)
                        if ok: st.success(msg); st.session_state.role = "operator"; st.rerun()
                        else: st.error(msg)

        st.divider()
        st.markdown("##### 🔧 重置密码 / 删除用户")
        if users_df.empty: st.info("请先在搜索框里找到目标用户。")
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
                    st.success(f"已重置 {target_email} 的密码为 123456，该用户还可自行修改 1 次。")
            with col2:
                if st.button("🗑️ 删除该用户"):
                    if target_email == st.session_state.user: st.error("不能删除自己")
                    elif target_is_perm: st.error("不能删除永久 Admin")
                    elif target_role == "admin" and not i_am_permanent: st.error("不能删除管理员账号，请先转让 Admin 权限")
                    else: delete_user(target_email); st.success(f"已删除：{target_email}"); st.rerun()

# ---------- 修改密码 ----------
if "🔑 修改密码" in tab_dict:
    with tab_dict["🔑 修改密码"]:
        st.subheader("🔑 修改我的密码")
        conn = get_conn(); cur = conn.cursor()
        cur.execute("SELECT password_change_count FROM app_users WHERE email = %s", (st.session_state.user,))
        cnt_row = cur.fetchone(); cur.close(); conn.close()
        change_count = cnt_row[0] if cnt_row else 0
        if change_count >= 2:
            st.warning("⚠️ 您已经修改过 2 次密码，不能再自行修改。如需再次修改，请联系管理员重置密码（重置后密码将变回 123456）。")
        else:
            st.caption(f"您还可以修改 {2 - change_count} 次密码（无需验证旧密码，只需核对身份信息）。")
            with st.form("change_pwd_form"):
                st.markdown("##### 1. 身份信息核验")
                col1, col2, col3 = st.columns(3)
                with col1: input_email = st.text_input("邮箱 *", value=st.session_state.user)
                with col2: input_name = st.text_input("姓名 *", value=st.session_state.name or "")
                with col3: input_student_id = st.text_input("学号 *", value=st.session_state.student_id or "")
                st.divider()
                st.markdown("##### 2. 设置新密码")
                col4, col5 = st.columns(2)
                with col4: new_pwd = st.text_input("新密码 *", type="password")
                with col5: confirm_pwd = st.text_input("确认新密码 *", type="password")
                if st.form_submit_button("确认修改", type="primary"):
                    if not all([input_email, input_name, input_student_id, new_pwd, confirm_pwd]): st.error("所有字段都必须填写")
                    elif new_pwd != confirm_pwd: st.error("两次输入的新密码不一致")
                    elif len(new_pwd) < 6: st.error("新密码长度不能少于 6 位")
                    else:
                        ok, msg = change_user_password(st.session_state.user, input_email, input_name, input_student_id, new_pwd)
                        if ok: st.success(msg); st.info("下次登录请使用新密码。"); st.rerun()
                        else: st.error(msg)