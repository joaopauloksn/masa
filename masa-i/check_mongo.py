from pymongo import MongoClient
import os

secret_dir = '/etc/secrets/mongo'
user = open(secret_dir + '/username').read().strip()
pwd  = open(secret_dir + '/password').read().strip()
host = os.environ.get('MONGO_HOST', 'mas-mongo-ce-0.mas-mongo-ce-svc.mongoce.svc.cluster.local:27017')

client = MongoClient(
    'mongodb://' + user + ':' + pwd + '@' + host,
    authMechanism='SCRAM-SHA-256', tls=True, tlsInsecure=True, authSource='admin'
)
db = client['mongo_scaling']

total = db.microservices.count_documents({})
pipeline = [{"$group": {"_id": "$label", "count": {"$sum": 1}}}, {"$sort": {"_id": 1}}]
dist = list(db.microservices.aggregate(pipeline))

print('--- MongoDB microservices collection ---')
print('Total documents:', total)
for x in dist:
    print(f'  label={x["_id"]}: {x["count"]} ({100*x["count"]//total if total else 0}%)')

# Show the 3 most recent entries
recent = list(db.microservices.find({}, {'label':1,'replicas':1,'m_quantile_90':1,'m_percentage__delivered':1,'timestamp':1}).sort('timestamp',-1).limit(3))
print('\nMost recent entries:')
for r in recent:
    print(f'  ts={r.get("timestamp")}  label={r.get("label")}  replicas={r.get("replicas")}  P90={r.get("m_quantile_90")}  del%={r.get("m_percentage__delivered")}')
