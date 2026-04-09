This is the testing of Computer and Communication Security and Cloud Computing project

You may following these steps to run the test.

0. Redis setup (Install Docker Desktop first)(This runs outside the VS Code)

	Open Docker Desktop

    Terminal: (First time -> Create Redis database)

	    docker run -d --name my-redis -p 6379:6379 redis

    Terminal: (Not first time -> Start the existing Redis)

        docker start my-redis

1. Activate listener requestHandler api listener

    Terminal:

	    uvicorn requestHandler:app --port 8000

2. Run requestHandler.py

3. Run verifierWorker.py

4. Run k6 to simulate request

    Terminal:
    	k6 run load_test.js

5. Record stats

    Observe Redis queue
        Terminal: Snap shot
            docker exec -it my-redis redis-cli LLEN proof_queue

        Terminal: Real-time
            docker exec -it my-redis redis-cli -r -1 -i 1 LLEN proof_queue