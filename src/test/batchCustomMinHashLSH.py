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
        #q_json = json.dumps({ "min_hash": q.min_hash, "lsh_hash": q.lsh_hash})
        rdb.hmset("news:{}".format(q.id), { "min_hash": ','.join(str(x) for x in q.min_hash),
                "lsh_hash": ','.join(str(x) for x in q.lsh_hash)})
        #if config.LOG_DEBUG: print(q_json)
        for tag in q.tag_company:
            rdb.zadd("lsh:{}".format(tag), int(q.timestamp), q_json)
            #rdb.sadd("lsh:{}".format(tag), q.id)
            rdb.sadd("lsh_keys", "lsh:{}".format(tag))
        #except Exception as e:
        #    print("ERROR: failed to save tag {0} to Redis".format(tag))


# Computes MinHashes, LSHes for all in DataFrame
def compute_minhash_lsh(df, mh, lsh):
    calc_min_hash = udf(lambda x: list(map(lambda x: int(x), mh.calc_min_hash_signature(x))), ArrayType(IntegerType()))
    calc_lsh_hash = udf(lambda x: list(map(lambda x: int(x), lsh.find_lsh_buckets(x))), ArrayType(IntegerType()))

    df = df.withColumn("min_hash", calc_min_hash("text_body_stemmed"))
    df = df.withColumn("lsh_hash", calc_lsh_hash("min_hash"))

    #if config.LOG_DEBUG: print(df.first())
    df.foreachPartition(store_lsh_redis_by_tag)
    #df.foreachPartition(lambda iter, rdb = redis.StrictRedis(config.REDIS_SERVER, port=6379, db=0): store_lsh_redis_by_tag(iter, rdb))
    return df


def get_jacc_sim_and_save_result_redis(candidate_set):
    rdb = redis.StrictRedis(config.REDIS_SERVER, port=6379, db=0)
    for idx, _b_id in enumerate(candidate_set):
        _base = rdb.hgetall('news:{}'.format(_b_id))
        _base['timestamp'] = int(_base['timestamp'])
        for _s_id in candidate_set[idx+1:]:
            temp1 = rdb.hget("jacc_sim", '{}:{}'.format(_b_id, _s_id))
            temp2 = rdb.hget("jacc_sim", '{}:{}'.format(_b_id, _s_id))
            if temp1 is not None:
                rdb.sadd("dup_cand:{}".format(_b_id), _s_id)
            elif temp2 is not None:
                rdb.sadd("dup_cand:{}".format(_s_id), _b_id)
            else:
                _sim  = rdb.hgetall('news:{}'.format(_s_id))
                _sim['timestamp'] = int(_sim['timestamp'])
                if abs(_base['timestamp'] - _sim['timestamp']) > config.TIME_WINDOW:
                    continue

                # base is a news which appear later
                if _base['timestamp'] < _sim['timestamp']:
                    b_id, s_id = _s_id, _b_id
                    b_min_hash = _base['min_hash'].split(",")
                    s_min_hash = _sim['min_hash'].split(",")
                else:
                    b_id, s_id = _b_id, _s_id
                    b_min_hash = _sim['min_hash'].split(",")
                    s_min_hash = _base['min_hash'].split(",")

                #calculate jaccard similarity and update redis cache
                jacc_sim = util.jaccard_sim_score(b_min_hash, s_min_hash)
                rdb.hset("jacc_sim", '{}:{}'.format(b_id, s_id), jacc_sim)

                # if jaccard_sim is above threshold, save as dup_cand to Redis
                if jacc_sim > config.DUP_QUESTION_MIN_HASH_THRESHOLD:
                    rdb.sadd("dup_cand:{}".format(b_id), s_id)
                    if config.LOG_DEBUG:
                        print('Dup candidate {}-{}: {}'.format( _base['headline'], _sim['headline'], jacc_sim))


def get_jaccard_similarity(candidate_set):
    """
    Input whole df to calculate similar sets base on candidate_set,
    create base set and its similar sets in a dictionary.
    return = {base_set:(similar_set:jaccard_similarity, )}
    """
    #start_time = time.time()
    rdb = redis.StrictRedis(config.REDIS_SERVER, port=6379, db=0)
    _similar_dict = {}
    #if config.LOG_DEBUG: print('get_jaccard_similarity=>candidate_set=%s'%(str(candidate_set)))
    for _b_set, _s_set in itertools.permutations(candidate_set,2):

        if int(_b_set[3]) < int(_s_set[3]) or int(_b_set[3]) > (int(_s_set[3]) + config.TIME_WINDOW):
            continue

        if config.LOG_DEBUG: print(_b_set, _s_set)

        #calculate jaccard similarity and update redis cache
        jaccard_sim_token = '{}:{}'.format(_b_set[0], _s_set[0])
        _jaccard_similarity = rdb.hget("jacc_sim", jaccard_sim_token)
        if _jaccard_similarity is None:
            _jaccard_similarity = util.jaccard_sim_score(_b_set[1], _s_set[1])
            rdb.hset("jacc_sim", jaccard_sim_token, _jaccard_similarity)
        #_jaccard_similarity = util.jaccard_sim_score(_b_set[1], _s_set[1])

        # Store the result and get top NUM_OF_MOST_SIMILAR_SET similar sets
        _jaccard_similarity = float(_jaccard_similarity)
        if _jaccard_similarity > config.DUP_QUESTION_MIN_HASH_THRESHOLD:
            if (_b_set[0],_b_set[2],_b_set[3]) not in _similar_dict:
                _similar_dict[(_b_set[0],_b_set[2],_b_set[3])] = []
            else:
                _similar_dict[(_b_set[0],_b_set[2],_b_set[3])].append([_jaccard_similarity, (_s_set[0],_s_set[2],_s_set[3]) ])

    # filter and select top similar set.
    # _similar_dict = dict( [(k,sorted(v, key=lambda x: (x[0],-x[1][2]), reverse=True)[:config.NUM_OF_MOST_SIMILAR_SET])
    #                     for k,v in _similar_dict.items() if len(v)>0])

    #if config.LOG_DEBUG: print('get_jaccard_similarity=> _similar_dict=%s'%(_similar_dict))
    #end_time = time.time()
    #if config.LOG_DEBUG: print("get_jaccard_similarity run time (seconds): {0} seconds".format(end_time - start_time))
    return _similar_dict


def _custom_extend(a,b): # both a, b are list
    a.extend(b)
    return a

def _merge_result(acc_list, value_list):
    # Remove redundant similar sets from each partitions
    output = []
    for _v in value_list+acc_list:
        if _v not in output: output.append(_v)
        #if config.LOG_DEBUG > 1: print('LSH.get_merge_result=> _final_dict=%s'%(_final_dict))
    return output

def _store_similar_cands_redis(similar_dict):
    """
    input - similar_dict:
        key: (id, headline, timestamp)
        val: [jaccard_sim,  (id, headline, timestamp)]
    """
    if config.LOG_DEBUG: print("========= Saving dup_cand to Redis ===========")
    rdb = redis.StrictRedis(config.REDIS_SERVER, port=6379, db=0)
    for cand in similar_dict:
        for sim in similar_dict[cand]:
            context = tuple(cand[1:] + sim[1] + (sim[0],))
            # Store order by jaccard_sim_score
            #rdb.zadd("dup_cand:{}".format(cand[0]), sim[0], val)
            rdb.sadd("dup_cand:{}".format(cand[0]), context)


def find_similar_cands_per_tag(tag, mh, lsh):
    rdb = redis.StrictRedis(config.REDIS_SERVER, port=6379, db=0)

    # get the dataframe for all news of given tag. id and lsh_hash columns loaded from Redis.
    tq = []
    # ids = rdb.smembers("lsh:{0}".format(tag))
    ids = rdb.zrangebyscore("lsh:{0}".format(tag), '-inf', '+inf', withscores=False)
    for id in ids:
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

    def _helperFunc(iterator):
        for cand in iterator:
            if cand[1] is not None and len(cand[1]) > 1:
                try:
                    get_jacc_sim_and_save_result_redis(cand[1])
                except Exception as e:
                    print("Error saving jaccard_sim result to Redis: {}".format(e))

    rdd_common_bucket = df.select(col('id'), col('lsh_hash')).rdd.flatMap(
        lambda x: (((hash, band), [x[0]]) for band, hash in enumerate(x[1]))).reduceByKey(
        lambda a, b: _custom_extend(a,b)).filter(lambda x: len(x[1])>1).foreachPartition(_helperFunc)

    # def _convert_hash_string_to_list(x):
    #     return [x[0],  x[1].split(',') if x[1] is not None else [], x[2], x[3],
    #         x[4].split(',') if x[4] is not None else [] ]
    #
    # rdd_common_bucket = df.select(col('id'), col('min_hash'), col('headline'),
    #     col('timestamp'), col('lsh_hash')).rdd.map(lambda x: _convert_hash_string_to_list(x)).flatMap(
    #     lambda x: (((hash, band), [(x[0], x[1], x[2], x[3])]) for band, hash in enumerate(x[4]))).reduceByKey(
    #     lambda a, b: _custom_extend(a,b)).filter(lambda t: len(t[1])>1).map(lambda t: tuple(t[1]))
    #if config.LOG_DEBUG: print('rdd_common_bucket: ', rdd_common_bucket.first())

    # rdd_cands = rdd_common_bucket.map(lambda cand_set: get_jaccard_similarity(cand_set))
    # #if config.LOG_DEBUG: print('rdd_cands: ', rdd_cands.first())
    #
    # similar_dict = rdd_cands.flatMap(lambda x: x.items()).reduceByKey(
    #         lambda acc, val: _merge_result(acc, val)).collectAsMap()
    # if config.LOG_DEBUG: print("find_similar_cands_lsh ==> {}".format(similar_dict))
    # _store_similar_cands_redis(similar_dict)



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

    # Compute pairwise LSH similarities for news within tags
    if (config.LOG_DEBUG):
        print("[BATCH]: Fetching questions,comparing LSH and MinHash, \
                uploading duplicate candidates back to Redis...")
    rdb = redis.StrictRedis(config.REDIS_SERVER, port=6379, db=0)

    # Fetch all tags from lsh_keys set
    for lsh_key in rdb.sscan_iter("lsh_keys", match="*", count=500):
        tag = lsh_key.replace("lsh:", "")
        tq_table_size = rdb.zcard("lsh:{0}".format(tag))
        if tq_table_size < 2: continue

        find_similar_cands_per_tag(tag, mh, lsh)

    end_time = time.time()
    print("Spark Custom MinHashLSH run time (seconds): {0} seconds".format(end_time - start_time))


if(__name__ == "__main__"):
    main()
