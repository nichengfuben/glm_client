# example.py
import asyncio
from main import *

async def main():
##    await initialize()
##    response = await chat("请介绍Python")
##    print(response)
    await initialize()
    
    questions = [
        "什么是Python？",
        "什么是JavaScript？",
        "什么是Go？"
    ]
    
    # 并发发送3个请求
    tasks = [chat(q) for q in questions]
    responses = await asyncio.gather(*tasks)
    
    for q, r in zip(questions, responses):
        print(f"{r}\n")
        
if __name__ == "__main__":
    asyncio.run(main())
