# init_db.py
from sqlalchemy import create_engine
from models import Base

# 注意：这里我们使用同步驱动 pymysql 来进行一次性建表，比较简单
# 如果没安装，请执行：pip install pymysql
DATABASE_URL_SYNC = "mysql+pymysql://root:root@localhost:3306/ai_assistant_db"

def init_database():
    # 1. 创建同步引擎
    engine = create_engine(DATABASE_URL_SYNC, echo=True)
    
    print("正在连接数据库并创建表...")
    
    # 2. 执行建表动作
    # 这行代码会寻找所有继承自 Base 的类，并在数据库中生成对应的 CREATE TABLE 语句
    Base.metadata.create_all(bind=engine)
    
    print("所有表已创建成功！")

if __name__ == "__main__":
    init_database()