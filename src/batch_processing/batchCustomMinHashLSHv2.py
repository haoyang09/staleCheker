import sys
import os
import time
import json
import pickle
import itertools
import redis

from pyspark.conf import SparkConf
from pyspark.context import SparkContext
from pyspark.sql import SQLContext
from pyspark.sql.functions import udf, col, size, collect_list
from pyspark.sql.types import IntegerType, ArrayType, StringType, Row
from pyspark.sql.functions import explode


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/config")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/lib")
import config
import util
import locality_sensitive_hash
import min_hash


def load_mh_lsh():
    #  Create and save MinHash and LSH if not exist or load them from file
    if(not os.path.isfile(config.MIN_HASH_PICKLE) or not os.path.isfile(config.LSH_PICKLE)):
        mh = min_hash.MinHash(config.MIN_HASH_K_VALUE)
        lsh = locality_sensitive_hash.LSH(config.LSH_NUM_BANDS, config.LSH_BAND_WIDTH, config.LSH_NUM_BUCKETS)
        print('saving mh, lsh to file {}, {}'.format(config.MIN_HASH_PICKLE, config.LSH_PICKLE))
        util.save_pickle_file(mh, config.MIN_HASH_PICKLE)
        util.save_pickle_file(lsh, config.LSH_PICKLE)
    else:
        if config.LOG_DEBUG: print('loading mh and lsh from local files')
        mh = util.load_pickle_file(config.MIN_HASH_PICKLE)
        lsh = util.load_pickle_file(config.LSH_PICKLE)
    if config.LOG_DEBUG: print('mh and lsh init finished')
    return mh, lsh


# Store news data
def store_lsh_redis_by_tag(iter):
    rdb = redis.StrictRedis(config.REDIS_SERVER, port=6379, db=0)
    if config.LOG_DEBUG: print("store minhash and lsh by company tag")
    for q in iter:
        if config.LOG_DEBUG: print(q.headline)
        rdb.hmset("news:{}".format(q.id),
                    {
                        "min_hash": ','.join(str(x) for x in q.min_hash),
                        "lsh_hash": ','.join(str(x) for x in q.lsh_hash)
                    }
                )

        for tag in q.tag_company:
            rdb.sadd("lsh:{}".format(tag), q.id)
            rdb.sadd("lsh_keys", "lsh:{}".format(tag))



# Computes MinHashes, LSHes for all in DataFrame
def compute_minhash_lsh(df, mh, lsh):
    calc_min_hash = udf(lambda x: list(map(lambda x: int(x), mh.calc_min_hash_signature(x))), ArrayType(IntegerType()))
    calc_lsh_hash = udf(lambda x: list(map(lambda x: int(x), lsh.find_lsh_buckets(x))), ArrayType(IntegerType()))

    df = df.withColumn("min_hash", calc_min_hash("text_body_stemmed"))
    df = df.withColumn("lsh_hash", calc_lsh_hash("min_hash"))

    df.foreachPartition(store_lsh_redis_by_tag)
    return df



def get_jacc_sim_and_save_result_redis(candidate_set):
    rdb = redis.StrictRedis(config.REDIS_SERVER, port=6379, db=0)
    for idx, _b_id in enumerate(candidate_set):
        _base = rdb.hgetall('news:{}'.format(_b_id))

        # skip those whose jacc_sim already calculated
        _s_ids = [iid for iid in candidate_set[idx+1:] if iid not in rdb.hgetall('jacc_sim:{}'.format(_b_id))]

        for _s_id in _s_ids:
            _sim  = rdb.hgetall('news:{}'.format(_s_id))

            # skip if timestamp difference is larger than time window
            if abs(int(_base['timestamp']) - int(_sim['timestamp'])) > config.TIME_WINDOW:
                continue

            # skip if _b_id is in for jacc_sim:_s_id
            if rdb.hget('jacc_sim:{}'.format(_s_id), _b_id) is not None:
                continue

            # base is the news which appear later
            (b_id, s_id) = (_s_id, _b_id) if int(_base['timestamp']) < int(_sim['timestamp']) else (_b_id, _s_id)

            #calculate jaccard similarity and update redis cache
            jacc_sim = util.jaccard_sim_score(_base['min_hash'].split(","), _sim['min_hash'].split(","))
            rdb.hset("jacc_sim:{}".format(b_id), s_id, jacc_sim)

            # if jaccard_sim is above threshold, save as dup_cand to Redis
            if jacc_sim > config.DUP_QUESTION_MIN_HASH_THRESHOLD:
                rdb.hset("dup_cand:{}".format(b_id), s_id, jacc_sim)
                if config.LOG_DEBUG:
                    print('Dup candidate {}, {}---{}'.format(jacc_sim, _base['headline'], _sim['headline']))



def find_similar_cands_per_tag(tag, mh, lsh):
    rdb = redis.StrictRedis(config.REDIS_SERVER, port=6379, db=0)

    def _custom_extend(a,b): # both a, b are list
        a.extend(b)
        return a

    def _helper(iterator):
        for cand in iterator:
            if cand[1] is not None and len(cand[1]) > 1:
                try:
                    get_jacc_sim_and_save_result_redis(cand[1])
                except Exception as e:
                    print("Error saving jaccard_sim result to Redis: {}".format(e))

    # get the dataframe for all news of given tag. id and lsh_hash columns loaded from Redis.
    tq = []
    for id in rdb.smembers("lsh:{}".format(tag)):
        lsh_hash = rdb.hget("news:{}".format(id), 'lsh_hash')
        if lsh_hash is not None:
            news = {}
            news['id'] = id
            news['lsh_hash'] = lsh_hash.split(',')
            tq.append(news)
        else:
            print("Failed to get lsh_hash for news:{}".format(id))

    if len(tq) < 2: return
    if config.LOG_DEBUG: print("tag {0}: {1} news".format(tag, len(tq)))

    df = sql_context.read.json(sc.parallelize(tq))
    df.select(col('id'), col('lsh_hash')).rdd\
            .flatMap( lambda x: (((hash, band), [x[0]]) for band, hash in enumerate(x[1])))\
            .reduceByKey( lambda a, b: _custom_extend(a,b))\
            .filter(lambda x: len(x[1])>1)\
            .foreachPartition(_helper)



def main():
    spark_conf = SparkConf().setAppName("Spark CustomMinHashLSH").set("spark.cores.max", "30")

    global sc
    sc = SparkContext(conf=spark_conf)
    sc.setLogLevel("ERROR")
    sc.addFile(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/lib/min_hash.py")
    sc.addFile(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/lib/locality_sensitive_hash.py")
    sc.addFile(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/lib/util.py")
    sc.addFile(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/config/config.py")
    global sql_context
    sql_context = SQLContext(sc)

    start_time = time.time()
    df = util.read_all_json_from_bucket(sql_context, config.S3_BUCKET_BATCH_PREPROCESSED) # load historical data
    mh, lsh = load_mh_lsh()
    compute_minhash_lsh(df, mh, lsh) # Compute MinHash/LSH hashes for historical news

    rdb = redis.StrictRedis(config.REDIS_SERVER, port=6379, db=0)

    # Fetch all tags from lsh_keys set
    for lsh_key in rdb.sscan_iter("lsh_keys", match="*", count=500):
        tag = lsh_key.replace("lsh:", "")
        #if tag == 'uns': continue
        tq_table_size = rdb.scard("lsh:{0}".format(tag))
        if tq_table_size < 2: continue

        find_similar_cands_per_tag(tag, mh, lsh)

    end_time = time.time()
    print("Spark Custom MinHashLSH run time (seconds): {0} seconds".format(end_time - start_time))


if(__name__ == "__main__"):
    main()