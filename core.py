import json
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from send import NotificationSender
import os
from pymongo import MongoClient
import cfscrape
import shutil

scraper = cfscrape.create_scraper()  # returns a CloudflareScraper instance

class ForumMonitor:
    def __init__(self, config_path='data/config.json'):
        self.config_path = config_path
        self.proxy_host = os.getenv("PROXY_HOST", None)  # 从环境变量读取代理配置
        self.mongo_host = os.getenv("MONGO_HOST", 'mongodb://localhost:27017/')  # 从环境变量读取代理配置
        self.load_config()

        # 连接到 MongoDB
        self.mongo_client = MongoClient(self.mongo_host)  # 默认连接到本地 MongoDB
        self.db = self.mongo_client['bf24']  # 使用数据库 'forum_monitor'
        self.threads_collection = self.db['threads']  # 线程集合
        self.comments_collection = self.db['comments']  # 评论集合
        try:
            # 创建索引。如果索引已经存在，MongoDB 会自动跳过创建，无需担心重复。
            self.threads_collection.create_index('link', unique=True)
            self.comments_collection.create_index('comment_id', unique=True)
        except Exception as e:
            print(e)

    # 加载配置文件
    def load_config(self):
        try:
            # 检查配置文件是否存在
            if not os.path.exists(self.config_path):
                print(f"{self.config_path} 不存在，复制到 {self.config_path}")
                try: os.mkdir('data')
                except: pass
                shutil.copy('example.json', self.config_path)
            with open(self.config_path, 'r') as f:
                self.config = json.load(f)['config']
                self.notifier = NotificationSender(self.config_path)  # 创建通知发送器
            print("配置文件加载成功")
        except Exception as e:
            print(f"加载配置失败: {e}")
            self.config = {}

    def workers_ai_run(self, model, inputs):
        headers = {"Authorization": f"Bearer {self.config['cf_token']}"}
        input = { "messages": inputs }
        response = requests.post(f"https://api.cloudflare.com/client/v4/accounts/{self.config['cf_account_id']}/ai/run/{model}", headers=headers, json=input)
        return response.json()

    # 用AI总结Thread
    def get_summarize_from_ai(self, description):
        inputs = [
            { "role": "system", "content": self.config['thread_prompt'] },
            { "role": "user", "content": description}
        ]

        output = self.workers_ai_run(self.config['model'], inputs)  # "@cf/qwen/qwen1.5-14b-chat-awq"
        return output['result']['response'].split('END')[0]

    # 用AI判断评论是否值得推送
    def get_filter_from_ai(self, description):
        inputs = [
            { "role": "system", "content": self.config['filter_prompt'] },
            { "role": "user", "content": description}
        ]

        output = self.workers_ai_run(self.config['model'], inputs)  # "@cf/qwen/qwen1.5-14b-chat-awq"
        return output['result']['response'].split('END')[0]

    def handle_thread(self, thread_data):
        existing_thread = self.threads_collection.find_one({'link': thread_data['link']})
        if not existing_thread:
            self.threads_collection.insert_one(thread_data)
            print(f"线程已存储: {thread_data['title']}, 链接: {thread_data['link']}")
            time_diff = datetime.utcnow() - thread_data['pub_date']
            if time_diff.total_seconds() <= 24 * 60 * 60:
                formatted_pub_date = thread_data['pub_date'].strftime("%Y/%m/%d %H:%M")
                summary = self.get_summarize_from_ai(thread_data['description'])
                message = (
                    f"{thread_data['cate'].upper()} 新促销\n"
                    f"标题：{thread_data['title']}\n"
                    f"作者：{thread_data['creator']}\n"
                    f"发布时间：{formatted_pub_date}\n\n"
                    f"{thread_data['description'][:200]}...\n\n"
                    f"{summary}\n\n"
                    f"{thread_data['link']}"
                )
                self.notifier.send_message(message)
        else:
            pass

    def handle_comment(self, comment_data, thread_data):
        existing_comment = self.comments_collection.find_one({'comment_id': comment_data['comment_id']})
        if not existing_comment:
            self.comments_collection.update_one(
                {'comment_id': comment_data['comment_id']},
                {'$set': comment_data},
                upsert=True
            )
            time_diff = datetime.utcnow() - comment_data['created_at']
            if time_diff.total_seconds() <= 24 * 60 * 60:
                ai_response = self.get_filter_from_ai(comment_data['message'])
                if "FALSE" not in ai_response:
                    formatted_pub_date = comment_data['created_at'].strftime("%Y/%m/%d %H:%M")
                    message = (
                        f"{thread_data['cate'].upper()} 新评论\n"
                        f"作者：{comment_data['author']}\n"
                        f"发布时间：{formatted_pub_date}\n\n"
                        f"{comment_data['message'][:200]}...\n"
                        f"{ai_response[:200]}...\n\n"
                        f"{comment_data['url']}"
                    )
                    self.notifier.send_message(message)
                else:
                    print(f'AI skip {comment_data["message"]}')

    # 检查 NDTN 评论
    def check_ndtn_comments(self, url="https://lowendtalk.com/profile/comments/NDTN"):
        print(f"正在检查 NDTN 评论: {url}")
        response = scraper.get(url)
        if response.status_code == 200:
            page_content = response.text
            self.parse_ndtn_comments(page_content)
        else:
            print(f"无法获取 NDTN 评论数据: {response.status_code}")

    # 解析 NDTN 评论页面
    def parse_ndtn_comments(self, page_content):
        soup = BeautifulSoup(page_content, 'html.parser')
        comments = soup.find_all('li', class_='ItemComment')
        for comment in comments:
            comment_classes = comment.get('class')
            if not comment_classes or 'Role_PatronProvider' not in comment_classes:
                continue
            comment_id = comment.get('id')
            if not comment_id:
                continue
            comment_id = comment_id.split('_')[1]
            author = comment.find('a', class_='Username').text.strip()
            message = comment.find('div', class_='Message').text.strip()
            created_at = comment.find('time')['datetime']
            if len(message) < 5:
                continue
            comment_data = {
                'comment_id': f'ndtn_{comment_id}',
                'author': author,
                'message': message[:200],
                'created_at': datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%S+00:00"),
                'url': f"https://lowendtalk.com/profile/comments/{comment_id}"
            }
            self.handle_comment(comment_data, {"cate": "ndtn", "link": f"https://lowendtalk.com/profile/comments/{comment_id}"})

    # 监控主循环
    def start_monitoring(self):
        print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 开始监控...")
        frequency = self.config.get('frequency', 600)
        debug = True

        while True:
            if debug:
                print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 开始遍历...")
                self.check_ndtn_comments()  # 检查 NDTN 评论
                print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 遍历完成...")
            else:
                try:
                    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 开始遍历...")
                    self.check_ndtn_comments()  # 检查 NDTN 评论
                    print(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 遍历完成...")
                except Exception as e:
                    print(f"检测过程出现错误: {e}")
            time.sleep(frequency)

    # 外部重载配置方法
    def reload(self):
        print("重新加载配置...")
        self.load_config()

# 示例运行
if __name__ == "__main__":
    monitor = ForumMonitor(config_path='data/config.json')
    monitor.start_monitoring()
