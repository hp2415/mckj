# init_db.py
from sqlalchemy import create_engine
from models import Base

DATABASE_URL_SYNC = "mysql+pymysql://root:root@localhost:3306/ai_assistant_db"

def init_database():
    engine = create_engine(DATABASE_URL_SYNC, echo=True)
    
    print("正在连接数据库并重置表...")
    
    # 每次运行前，先删除旧表，再创建新表，保证字段完全一致
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    
    print("所有表已重建成功！")

if __name__ == "__main__":
    init_database()