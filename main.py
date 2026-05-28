import os, re, random, json, time, requests, urllib.parse
import subprocess
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

# 禁用警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- 1. 通知与备份函数 ---

def send_wechat(title, content):
    """通过 Server酱 推送消息"""
    push_key = os.getenv("PUSH_KEY")
    if not push_key:
        print("⚠️ 未配置 PUSH_KEY，取消微信推送")
        return

    url = f"https://sctapi.ftqq.com/{push_key}.send"
    data = {"title": title, "desp": content}
    try:
        res = requests.post(url, data=data, timeout=10)
        if res.status_code == 200:
            print("🔔 微信通知发送成功")
    except Exception as e:
        print(f"❌ 微信通知发送失败: {e}")

def git_push_backup(count):
    """阶段性强制备份"""
    try:
        subprocess.run(["git", "config", "--local", "user.email", "action@github.com"], check=True)
        subprocess.run(["git", "config", "--local", "user.name", "GitHub Action"], check=True)
        subprocess.run(["git", "add", "."], check=True)
        msg = f"自动备份: 累计新增 {count} 条资源并同步配置"
        subprocess.run(["git", "commit", "-m", msg], check=False)
        print("🔄 正在同步远程仓库状态...")
        subprocess.run(["git", "pull", "origin", "main", "--rebase"], check=True)
        subprocess.run(["git", "push", "origin", "main"], check=True)
        print(f"🚀 [同步成功] 数据已推送至仓库")
    except Exception as e:
        print(f"⚠️ [同步跳过] 遇到冲突或网络问题: {e}")
        subprocess.run(["git", "rebase", "--abort"], check=False)


# --- 2. 网络会话设置 ---
def get_stable_session(base_url):
    session = requests.Session()
    retries = Retry(total=5, backoff_factor=1, status_forcelist=[500, 502, 503, 504])
    session.mount('http://', HTTPAdapter(max_retries=retries))
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": base_url
    })
    return session


# --- 3. 存盘逻辑 (倒序去重版) ---
def save_and_update(path, new_lines, db_list, db_path):
    """
    倒序排列：今天新抓的在最前面
    """
    items_dict = {}
    
    # 1. 加载旧数据
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
            blocks = re.findall(r'(#EXTINF:.*?)(?=#EXTINF:|$)', content, re.S)
            for block in blocks:
                clean_block = block.strip()
                if clean_block:
                    title_line = clean_block.split('\n')[0].strip()
                    items_dict[title_line] = clean_block

    # 2. 合并新数据 (新数据覆盖旧数据，保持 title 唯一)
    for item in new_lines:
        item = item.strip()
        if item:
            title_line = item.split('\n')[0].strip()
            items_dict[title_line] = item

    # 3. 排序逻辑：最新的日期排在前面
    sorted_keys = sorted(items_dict.keys(), reverse=True) 

    # 4. 写入文件
    with open(path, 'w', encoding='utf-8') as f:
        f.write("#EXTM3U\n")
        for k in sorted_keys:
            f.write(items_dict[k] + "\n")
    
    # 更新 JSON 数据库
    with open(db_path, 'w', encoding='utf-8') as f:
        json.dump(db_list, f, ensure_ascii=False, indent=4)


# --- 4. 核心收割逻辑 ---
def crawl_category(cat, session, base_url, stop_days):
    cat_id, cat_name = cat["id"], cat["name"]
    db_file = f"./{cat_name}.json"
    save_dir = f"./VideoResults/{cat_name}"
    save_path = f"{save_dir}/{cat_name}.m3u8"
    os.makedirs(save_dir, exist_ok=True)
    
    db = json.load(open(db_file, 'r', encoding='utf-8')) if os.path.exists(db_file) else []
    db_set = set(str(i) for i in db)
    
    print(f"\n📂 启动新站分类: 【{cat_name}】(ID: {cat_id}) | 库内: {len(db_set)}")
    stats = {"new": 0, "existed": len(db_set)}
    all_new_entries = []
    
    # 🎯 核心修复 1：使用传进来的真实 stop_days (10000) 来计算阈值
    stop_date_threshold = (datetime.now() - timedelta(days=stop_days)).strftime("%m-%d")

    # 🎯 核心修复 2：强力保险！如果天数设置大于一年，直接将截止日期抹平为 "00-00"
    # 这样新站遇到任何历史日期 (比如 05-26) 都绝对不会再误触发踩刹车
    if stop_days > 365:
        stop_date_threshold = "00-00"

    try:
        for p in range(1, 10000):
            # 🎯 适配新站翻页路径
            url = f"{base_url}index.php/vod/type/id/{cat_id}/page/{p}.html"
        
            try:
                res = session.get(url, timeout=15)
                if res.status_code >= 500:
                    print(f"⚠️ 新站服务器响应异常 (Status: {res.status_code})，触发避险中断...")
                    break
                res.encoding = 'utf-8'
                soup = BeautifulSoup(res.text, 'html.parser')
                
                # 精准锁定列表容器
                items = soup.find_all('li', class_='thumb')
                if not items: 
                    print(f"🏁 第 {p} 页未发现任何列表项，判定分类结束。")
                    break
                
                print(f"🌐 正在扫描第 {p} 页...")
                found_old_content = False
                
                for li in items:
                    title_tag = li.find('a')
                    if not title_tag: continue
                    
                    # 提取封面图和标题
                    img_tag = li.find('img')
                    title = img_tag.get('alt', '').strip() if img_tag else ""
                    cover_url = img_tag.get('data-original', '').strip() if img_tag else ""
                    
                    href = title_tag.get('href', '')
                    if not title or not href: continue

                    # --- 精准提取新站日期标签 ---
                    date_val = "01-01"
                    date_tag = li.find('span', class_='added')
                    if date_tag:
                        date_text = date_tag.get_text(strip=True)
                        date_matches = re.findall(r'(\d{2}-\d{2})', date_text)
                        if date_matches:
                            date_val = date_matches[-1]

                    # --- 4. 截止判定 ---
                    if p > 3 and date_val != "01-01":
                        if date_val < stop_date_threshold:
                            print(f"⏱️ 探测到旧日期 {date_val}，收割完成。")
                            found_old_content = True
                            break


                    # --- 去重判定 ---
                    v_id_match = re.search(r'id/(\d+)', href)
                    if not v_id_match: continue
                    v_id = v_id_match.group(1)
                    if v_id in db_set:
                        continue

                    # --- 捕获内层播放页面的 player_data JSON 数据 ---
                    try:
                        play_link = urllib.parse.urljoin(base_url, href)
                        p_res = session.get(play_link, timeout=12)
                        # 🎯 直接提取 player_data 的明文 m3u8 地址
                        m3u8_json_match = re.search(r'player_data\s*=\s*(\{.*?\})', p_res.text)
                        if m3u8_json_match:
                            try:
                                json_data = json.loads(m3u8_json_match.group(1))
                                m3u8 = json_data.get("url", "").replace('\\/', '/').replace('\\', '')
                            except:
                                m3u8_match = re.search(r'"url"\s*:\s*"([^"]+\.m3u8[^"]*)"', p_res.text, re.I)
                                m3u8 = m3u8_match.group(1).replace('\\/', '/').replace('\\', '') if m3u8_match else ""

                            if m3u8 and m3u8.startswith('http'):
                                if "%" in m3u8:
                                    m3u8 = urllib.parse.unquote(m3u8)

                                item_entry = f'#EXTINF:-1 tvg-logo="{cover_url}",{title} [{date_val}]\n{m3u8}\n'
                                all_new_entries.append(item_entry)
                                db.append(v_id)
                                db_set.add(v_id)
                                stats["new"] += 1
                                print(f"   ✅ [嗅探成功] {date_val} | {title[:15]}...")
                                
                                if stats["new"] > 0 and stats["new"] % 1000 == 0:
                                    print(f"📦 累计 1000 条，同步中...")
                                    save_and_update(save_path, all_new_entries, db, db_file)
                                    git_push_backup(stats["new"])
                                    all_new_entries = [] 
                    except Exception as e:
                        continue

                if found_old_content: break
                time.sleep(1.5)

            except Exception as e:
                print(f"  🚨 页面出错: {e}")
                break

    except KeyboardInterrupt:
        print("\n\n🛑 检测到手动中断（Ctrl+C）！正在准备紧急存盘...")
    
    finally:
        if all_new_entries:
            print(f"💾 正在写入缓存中的 {len(all_new_entries)} 条资源至硬盘...")
            save_and_update(save_path, all_new_entries, db, db_file)
        else:
            print("ℹ️ 无新数据需要保存。")
        
    return stats


# --- 5. E2 Bouquet 转换 ---
def convert_to_e2_bouquets():
    BASE_DIR = './VideoResults'
    OUTPUT_DIR = './E2_Bouquets'
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    if not os.path.exists(BASE_DIR): return
    categories = [d for d in os.listdir(BASE_DIR) if os.path.isdir(os.path.join(BASE_DIR, d))]

    for idx, cat_name in enumerate(categories):
        m3u8_path = os.path.join(BASE_DIR, cat_name, f"{cat_name}.m3u8")
        if not os.path.exists(m3u8_path): continue
        with open(m3u8_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        items = content.split("#EXTINF")
        output_lines = [f"#NAME {cat_name}"]
        
        sid = 1
        hex_id = hex(200 + idx)[2:].upper() 
        
        for item in items:
            if not item.strip(): continue
            lines = item.strip().split('\n')
            title = lines[0].split(',')[-1].strip()
            url = lines[-1].strip()
            
            if url.startswith('http'):
                h_sid = hex(sid)[2:].upper().zfill(4)
                escaped_url = url.replace(':', '%3a')
                output_lines.append(f"#SERVICE 4097:0:1:{h_sid}:{hex_id}:0:0:0:0:0:{escaped_url}:{title}")
                output_lines.append(f"#DESCRIPTION {title}")
                sid += 1
        
        output_path = os.path.join(OUTPUT_DIR, f"subbouquet.{cat_name}.tv")
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("\n".join(output_lines) + "\n")
            
    print(f"📺 [E2 转换成功] 已在 {OUTPUT_DIR} 目录下生成对应的机顶盒电视节目单！")


# --- 6. 新站配置独立加载函数 ---
def load_config_new():
    config_path = "config_new.json"
    
    default_config = {
        "BASE_URL": "http://dpgc4.motorcycles/cn/home/web/",
        "CATS": [
            {"id": "20", "name": "国产自拍"},
            {"id": "21", "name": "强奸乱伦"},
            {"id": "22", "name": "男同女同"},
            {"id": "23", "name": "重口味"},
            {"id": "24", "name": "日本AV"},
            {"id": "25", "name": "无码视频"},
            {"id": "26", "name": "有码视频"},
            {"id": "27", "name": "中文字幕"},
            {"id": "28", "name": "欧美极品"},
            {"id": "29", "name": "三级伦理"},
            {"id": "30", "name": "动漫精品"}
        ],
        "STOP_DAYS_AGO": 1
    }
    
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return default_config
    else:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(default_config, f, indent=4, ensure_ascii=False)
        return default_config


# --- 7. 主程序入口 ---
if __name__ == "__main__":
    start_time = time.time()
    
    # 载入独立新站配置
    config = load_config_new() 
    BASE_URL = config["BASE_URL"] 
    STOP_DAYS = config.get("STOP_DAYS_AGO", 1)
    
    # 创建稳定网络流
    session = get_stable_session(BASE_URL)
    
    report = []
    print(f"🚀 启动【新站】专用收割程序")
    print(f"🔗 目标基础URL: {BASE_URL}")
    print(f"⏱️ 截止时间阈值: 扫描最近 {STOP_DAYS} 天的更新")
    print("-" * 50)
    
    try:
        for cat in config.get("CATS", []):
            try:
                res = crawl_category(cat, session, BASE_URL, STOP_DAYS)
                report.append({"name": cat["name"], **res})
            except KeyboardInterrupt:
                print(f"\n⚠️ 手动跳过分类: {cat['name']}")
                continue
            except Exception as e:
                print(f"❌ 分类 {cat['name']} 运行出错: {e}")
                continue
                
    except Exception as e:
        print(f"💥 主程序严重异常: {e}")
        
    finally:
        print(f"\n{'='*30}\n收割总结 (今日日期: {datetime.now().strftime('%m-%d')})\n{'='*30}")
        
        # 触发转换程序
        try:
            convert_to_e2_bouquets()
        except Exception as e:
            print(f"⚠️ E2 节目单转换失败: {e}")

        if 'report' in locals() and report:
            total_all = sum(r.get('new', 0) for r in report if isinstance(r, dict))
            summary_text = "\n".join([f"- {r['name']}: +{r['new']}" for r in report])
            
            print(f"📊 详细汇总:\n{summary_text}")
            
            if total_all > 0:
                if os.getenv("GITHUB_ACTIONS") == "true":
                    msg_title = f"🚀 今日【新站】收割完成！新增 {total_all} 条"
                    msg_content = f"### 📥 新站自动收割汇总\n\n{summary_text}\n\n---\n📅 结束时间：{datetime.now().strftime('%m-%d %H:%M')}"
                    send_wechat(msg_title, msg_content)
                    git_push_backup(total_all)
                else:
                    print(f"🏠 本地运行结束，今日新站斩获 {total_all} 条。")
        else:
            print("ℹ️ 任务结束，未生成新数据报告。")

        print(f"✅ 流程全部结束，耗时: {time.time()-start_time:.1f}s")
