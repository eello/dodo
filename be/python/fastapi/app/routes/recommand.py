#-*- coding: utf-8 -*-

import json
import redis
import random
import logging
import time
import datetime
import pandas as pd
import numpy as np
from fastapi import Depends, APIRouter, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from config import conn, redis_config
from database.models import Category, Preference, PublicBucket, User, BucketListMember, BucketList, AddedBucket
from auth.auth_handler import decodeJWT
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.model_selection import train_test_split
from surprise import Dataset, SVD, accuracy, Reader
from database.schemas import *
# from surprise.model_selection import train_test_split


router = APIRouter()

engine = conn()

security = HTTPBearer()

# logging
logging.config.fileConfig("logging.conf", disable_existing_loggers=False)
logger = logging.getLogger(__name__)

@router.get('/test')
def session_test(db: Session = Depends(engine.get_session)):
	example = db.query(Category).all()
	return example

@router.get('/redistest')
def redis_test():
	rd = redis_config()
	rd.set("juice", "orange") # set
	
	return {
	    "data": rd.get("juice") # get
	}

# Preference - title을 활용한 CBF - 코사인 유사도 활용
@router.get("/buckets", status_code=200)
def bucket_recommand_cbf(bucketlist: int = 0, category: str = "전체", page: int = 0, size: int = 20,
		    db: Session = Depends(engine.get_session), 
	      	credentials: HTTPAuthorizationCredentials= Depends(security)):
	
	rd = redis_config()

	skip = size*page
	limit = size*page+size
	logger.info(f"page: {page}, size: {size}")
	logger.info(f"skip: {skip}, limit: {limit}")

	logger.info(f"버킷리스트 정보: {bucketlist}")
	logger.info(f"카테고리 정보: {category}")
	logger.info(credentials)
	token = decodeJWT(credentials.credentials)
	
	if('message' in token):
		raise HTTPException(status_code=401, detail="token is not valid")
	
	user_seq=token['userSeq']
	logger.info(f"LOGIN 정보: {user_seq}")

	category_seq = {"전체" : 0, "대자연" : 1, "일상" : 2, "쇼핑" : 3, "여행" :4, "문화예술" : 5, "자기계발" : 6, "푸드" : 7, "아웃도어" : 8, "스포츠" : 9}
	
	try:
		search_category_seq = category_seq[category]
	except:
		raise HTTPException(status_code=400, detail="parameter is not valid")
	
	logger.info(f"카테고리 seq: {search_category_seq}")

	prefer_data = db.query(PublicBucket.title, PublicBucket.category_seq)\
			.filter(Preference.user_seq == user_seq)\
			.filter(Preference.is_delete == 0)\
			.filter(Preference.bucket_seq == PublicBucket.seq)\
			.filter(PublicBucket.category_seq != 'null')\
			.all()
	
	logger.info(f"prefer_data: {prefer_data}")

	# 추천할만한 prefer data가 없는 경우
	if(len(prefer_data) == 0):
		logger.info("prefer data 부족, random recomm")
		response = bucket_random_recomm(db, user_seq, size, page, search_category_seq)
		return response
	
	pb_data = db.query(PublicBucket.emoji, PublicBucket.title, PublicBucket.added_count, PublicBucket.seq.label("bucket_seq"), Category.seq.label("category_seq"), Category.item)\
			.filter(PublicBucket.is_public == 1)\
			.filter(PublicBucket.category_seq == Category.seq)\
			.filter(PublicBucket.category_seq != 'null')\
			.all()

	logger.info(f"prefer_data 개수 : {len(prefer_data)}")
	logger.info(f"public bucket data 개수 : {len(pb_data)}")

	if(len(pb_data) == 0 or len(prefer_data) == 0):
		response = bucket_random_recomm(db, user_seq, size, page)
		return response
	
	if(len(prefer_data) > 3):
		endpoint = "buckets/" + str(user_seq) + "/" + "over/" + str(search_category_seq)
	else: 
		endpoint = "buckets/" + str(user_seq) + "/" + "under/" + str(search_category_seq)

	cache_size = rd.llen(endpoint)
	if(cache_size != 0):
		logger.info(f"redis cache O: {endpoint}")
		response = get_response(endpoint, size, page, cache_size, db, bucketlist)
		return response

	logger.info("redis cache X")

	# TODO 유저가 몇명 이상이면 협업 필터링을 해야할까?
	user_sum = db.query(User).count()
	prefer_sum = db.query(Preference).filter(Preference.bucket_seq == PublicBucket.seq).filter(PublicBucket.is_public == 1)\
			.filter(PublicBucket.is_delete == 0).filter(PublicBucket.category_seq != 'null').count()
	sql_query = select(Preference.user_seq, func.count(PublicBucket.title).label("counting")).join_from(PublicBucket, Preference)\
			.where(Preference.bucket_seq == PublicBucket.seq and Preference.is_delete == 0 and PublicBucket.is_delete == 1)\
			.group_by(Preference.user_seq).having(func.count(PublicBucket.title) >= 5)
	prefer_sum = len(db.execute(sql_query).all())
	logger.info(f"유저 수: {user_sum}")
	logger.info(f"5개 이상 선호도를 가진 유저 수: {prefer_sum}")


	# 협업필터링(CF)
	# if(user_sum >= 10 and (user_sum == prefer_sum)):
		
	# 	endpoint = "buckets/" + str(user_seq) + "/" + "cf/" + str(search_category_seq)
	# 	cache_size = rd.llen(endpoint)
		
	# 	if(cache_size != 0):
	# 		logger.info(f"redis cache O: {endpoint}")
	# 		response = get_response(endpoint, size, page, cache_size)
	# 		return response

	# 	response = bucket_recommand_cf(prefer_data, pb_data, user_seq, page, size, search_category_seq)
		
	# 	if(type(response) == str):
	# 		response = bucket_random_recomm(db, user_seq, size, page, search_category_seq)
	# 		return response
	# 	else:
	# 		return response


	# json 형태로 변환
	# pb_data = jsonable_encoder(pb_data)
	# print(data[0]['title'])

	# DataFrame 형태로 변환
	data = pd.DataFrame(pb_data)
	# print(data.head(2))

	logger.info(f"title 열의 결측값의 수: {data['title'].isnull().sum()}")

	stop_words=['하기']

	tfidf = TfidfVectorizer(stop_words=stop_words)
	tfidf_matrix = tfidf.fit_transform(data['title'])
	# print(tfidf)
	logger.info(f"TF-IDF 행렬의 크기(shape): {tfidf_matrix.shape}")

	cosine_sim = cosine_similarity(tfidf_matrix, tfidf_matrix)
	logger.info(f"코사인 유사도 연산 결과: {cosine_sim.shape}")

	title_to_index = dict(zip(data['title'], data.index))

	# idx = (title_to_index['등산하면서 경치 구경하기'])
	# print(idx)

	# 리스트별 버킷
	bucketlist_data = db.query(PublicBucket.title)\
			.filter(BucketList.seq == bucketlist)\
			.filter(BucketList.seq == AddedBucket.bucketlist_seq)\
			.filter(AddedBucket.bucket_seq == PublicBucket.seq)\
			.filter(AddedBucket.is_delete == 0)\
			.filter(PublicBucket.category_seq != 'null')\
			.all()

	list_prefer_data = []

	for i in bucketlist_data:
		list_prefer_data.append(i.title)
	
	# logger.info(f"prefernce title data list: {list_prefer_data}")

	# 추천 함수
	def get_recommendations(title, cosine_sim = cosine_sim):
		try:
			idx = title_to_index[title]
		except:
			result = pd.DataFrame()
			return result
		logger.info(f"idx 정보 : {idx}")

		sim_scores = list(enumerate(cosine_sim[idx]))

		# 유사도에 따라 버킷리스트들을 정렬한다.
		sim_scores = sorted(sim_scores, key=lambda x: x[1], reverse=True)

		# 인덱스 skip부터 skip+limt까지의 가장 유사한 버킷리스트를 받아온다.
		# sim_scores = sim_scores[page:page+size]
		# TODO 0개부터 8000개의 버킷
		sim_scores = sim_scores[0:8000]
		# print(sim_scores)

		# 가장 유사한 인덱스 skip부터 skip+limt까지의 버킷리스트의 인덱스를 얻는다.
		bucket_indices = [idx[0] for idx in sim_scores]
		# print(bucket_indices)
		
		result = data.loc[bucket_indices]
		# print(result)

		# 가장 유사한 인덱스 skip부터 skip+limt까지의 버킷리스트 객체들을 리턴한다.
		return result

	
 	# prefer_data 3개 초과인 경우 
	if(len(prefer_data) > 3) :
		logger.info(f"prefer_data 개수: {len(prefer_data)}")
		
		random_prefer = random.sample(range(len(prefer_data)), 3)
		logger.info(f"random_prefer: {random_prefer}")

		result = pd.DataFrame()

		for i in random_prefer:
			logger.info(f"random select title: {prefer_data[i].title}")
			temp = get_recommendations(prefer_data[i].title)

			# print(temp)

			result = pd.concat([result, temp])
		
		
		result = result.drop_duplicates(['title'])
		
		# temp = result.drop_duplicates(subset=['등산하고 경치 구경하기'])
		# TODO 정렬 넣을지 말지
		# result = result.sort_index()
		# print(search_category_seq)
		# print(result)
		
		# 카테고리 별 검색
		if(search_category_seq != 0):
			result = result[result.category_seq == search_category_seq]

		result = result.to_dict('records')

		temp_result = []
		
		for i in result:
			is_added = i['title'] in list_prefer_data
			category = Category_dto(i['category_seq'], i['item'])
			temp = Bucket_recoomm_dto(i['title'], i['emoji'], i['added_count'], i['bucket_seq'], is_added, category)
			temp_result.append(temp)

		endpoint = "buckets/" + str(user_seq) + "/" + "over/" + str(search_category_seq)
		logger.info(f"redis endpoint: {endpoint}")

		for i in temp_result:
			rd.rpush(endpoint, json.dumps(i, default=lambda x: x.__dict__, ensure_ascii=False).encode('utf-8'))
		rd.expire(endpoint, 180)
		logger.info(f"response data size: {len(temp_result[skip:limit])}")

		# data = {"content": temp_result[skip:limit]}
		data = {"content": temp_result[skip:limit], "last": len(temp_result) <= limit, "size": size, "number": page, "empty": len(temp_result) == 0}
		response = {"data": data, "success": True}

		return response


	# prefer_data 3개 이하인 경우
	else:
		logger.info(f"prefer_data 개수: {len(prefer_data)}")
		
		result = pd.DataFrame()

		for i in range(len(prefer_data)):
			logger.info(f"select title: {prefer_data[i].title}")
			temp = get_recommendations(prefer_data[i].title)

			result = pd.concat([result, temp])

		result = result.drop_duplicates(['title'])
		# print(result)

		# TODO 정렬 넣을지 말지
		# result = result.sort_index()

		# 카테고리 별 검색
		if(search_category_seq != 0):
			result = result[result.category_seq == search_category_seq]
		
		result = result.to_dict('records')

		temp_result = []

		for i in result:
			is_added = i['title'] in list_prefer_data
			category = Category_dto(i['category_seq'], i['item'])
			temp = Bucket_recoomm_dto(i['title'], i['emoji'], i['added_count'], i['bucket_seq'], is_added, category)
			temp_result.append(temp)

		endpoint = "buckets/" + str(user_seq) + "/" + "under/" + str(search_category_seq)
		logger.info(f"redis endpoint: {endpoint}")

		for i in temp_result:
			rd.rpush(endpoint, json.dumps(i, default=lambda x: x.__dict__, ensure_ascii=False).encode('utf-8'))
		rd.expire(endpoint, 180)
		logger.info(f"response data size: {len(temp_result[skip:limit])}")

		data = {"content": temp_result[skip:limit], "last": len(temp_result) <= limit, "size": size, "number": page, "empty": len(temp_result) == 0}

		response = {"data": data, "success": True}

		return response


# 코사인 유사도 - 사용자 간의 유사도 계산 후 유사도 높은 사용자의 버킷리스트 추천
@router.get("/social/bucketlists", status_code=200)
def user_recommand_cf(page: int = 0, size: int = 4,
		    db: Session = Depends(engine.get_session), 
	      	credentials: HTTPAuthorizationCredentials= Depends(security)):
	
	rd = redis_config()

	skip = size*page
	limit = size*page+size
	logger.info(f"page: {page}, size: {size}")
	logger.info(f"skip: {skip}, limit: {limit}")

	logger.info(credentials)
	token = decodeJWT(credentials.credentials)
	
	logger.info(token)
	
	if('message' in token):
		raise HTTPException(status_code=401, detail="token is no valid")
	
	userSeq=token['userSeq']
	logger.info(f"LOGIN 정보: {userSeq}")

	endpoint = "social/" + str(userSeq) + "/recomm"
	cache_size = rd.llen(endpoint)

	if(cache_size != 0):
		logger.info(f"redis cache O: {endpoint}")
		response = get_response(endpoint, size, page, cache_size)
		return response

	prefer_data = db.query(Preference)\
		.filter(Preference.is_delete == 0)\
		.filter(Preference.user_seq.in_(\
		db.query(Preference.user_seq).group_by(Preference.user_seq).having(func.count(Preference.user_seq) > 1)))\
		.all()
	
	pb_data = db.query(PublicBucket).filter(PublicBucket.is_public == 1).all()

	prefer_sum = db.query(Preference).filter(Preference.user_seq == userSeq).filter(Preference.is_delete == 0).count()
	logger.info(f"{userSeq}의 preferences 개수: {prefer_sum}")

	logger.info(f"prefer_data 개수 : {len(prefer_data)}")

	if(len(pb_data) == 0):
		raise HTTPException(status_code=400, detail="bucket data is null")

	user_sum = db.query(User).count()
	logger.info(f"유저 수: {user_sum}")

	if(user_sum <= 1):
		raise HTTPException(status_code=400, detail="user data is null")

	# 선호 데이터가 없는 경우 랜덤으로 추천
	if(prefer_sum == 0):
		response = social_random_recomm(db, userSeq, size, page)
		logger.info(f"response size: {len(response['data'])}")
		return response

	# json 형태로 변환
	prefer_data = jsonable_encoder(prefer_data)
	pb_data = jsonable_encoder(pb_data)

	# DataFrame 형태로 변환
	prefer_data = pd.DataFrame(prefer_data)
	pb_data = pd.DataFrame(pb_data)

	# is_delete 값을 rating으로 사용
	prefer_data['is_delete'] = prefer_data['is_delete']+1
	# if(prefer_data['is_delete'] == 1):
	# 	prefer_data['is_delete']+1

	# print(pb_data.head(3))
	# print(prefer_data.head(3))

	reader = Reader(rating_scale=(0.5, 5.0))
	temp = Dataset.load_from_df(prefer_data[['seq', 'bucket_seq', 'user_seq']], reader)
	
	userlen = len(prefer_data["user_seq"].unique())
	pblen = len(pb_data["seq"].unique())

	logger.info(f"고유 아이디 수: {userlen}")
	logger.info(f"공개된 버킷리스트 수: {pblen}")

	# index = user_seq, column = bucket_seq 행렬 만들기
	x = prefer_data.copy()
	y = prefer_data['user_seq']

	iteration = np.arange(0.20, 1.00, 0.01)

	# x_train, x_test, y_train, y_test = train_test_split(x, y, test_size=0.6, stratify=y, random_state=0)
	# x_train = x_train.reset_index(drop=True)
	# prefer_matrix = x_train.pivot(values='is_delete', index='user_seq', columns='bucket_seq')

	global a
	a = "train_test_split"

	for i in iteration:
		try:
			logger.info(f"try test size: {round(i, 5)}")
			x_train, x_test, y_train, y_test = train_test_split(x, y, test_size=round(i, 5), stratify=y, random_state=0)
			logger.info(f"success {a}")
			a = "pivot"
			x_train = x_train.reset_index(drop=True)
			# test_size = 0.25, 25% 랜덤 데이터가 x_test로 추출됨
			prefer_matrix = x_train.pivot(values='is_delete', index='user_seq', columns='bucket_seq')
			logger.info(f"success {a}")
			break
		except:
			logger.info(f"fail {a}")
			a = "train_test_split"
			if(i >= 0.60):
				response = social_random_recomm(db, userSeq, size, page)
				logger.info(f"response size: {len(response['data'])}")
				return response
				# raise HTTPException(status_code=400, detail="too low data")
			pass

	
	# print(prefer_matrix)

	# user sim matrix
	matrix_dummy = prefer_matrix.copy().fillna(0)
	user_sim = cosine_similarity(matrix_dummy, matrix_dummy)
	user_sim = pd.DataFrame(user_sim, index=prefer_matrix.index, columns=prefer_matrix.index)

	# temp = user_sim[['user_seq', userSeq]]
	# print(temp)

	user_list = user_sim[userSeq].sort_values(ascending=False)
	user_list = pd.DataFrame(user_list)

	print("--------유사도 출력--------")
	print(user_list)
	print("----------------")

	logger.info(f"user list length: {len(user_list)}")

	result = []

	# TODO 0개부터 100명의 유저
	if(len(user_list) > 100): user_sim_size = 100
	else: user_sim_size = len(user_list)-1

	logger.info(user_sim_size)

	# 유저 100명까지의 유사도
	for i in range(0, user_sim_size):
		now_user_seq = user_list.index[i+1] 
		logger.info(f"now_user_seq: {now_user_seq}")

		user_data = db.query(BucketList.title.label('bucketListTitle'), BucketList.image.label('bucketListImage'), \
		       User.profile_image.label('UserProfileImage'), User.nickname.label('UserProfileNickname'), \
				Category.item.label('CategoryItem'), \
				AddedBucket.emoji.label('BucketEmoji'), PublicBucket.title.label('BucketTitle'))\
			.filter(BucketListMember.bucketlist_seq == BucketList.seq)\
			.filter(BucketList.type == 'SINGLE')\
			.filter(BucketListMember.user_seq == now_user_seq)\
			.filter(AddedBucket.bucketlist_seq == BucketList.seq)\
			.filter(AddedBucket.bucket_seq == PublicBucket.seq)\
			.filter(BucketListMember.user_seq == User.seq)\
			.filter(AddedBucket.is_delete == 0)\
			.filter(BucketList.is_public == 1)\
			.filter(PublicBucket.category_seq == Category.seq)\
			.all()
		

		if (len(user_data) != 0):
			
			buckets = []

			for j in user_data:
				temp = Bucket_dto(j.BucketTitle, j.BucketEmoji, j.CategoryItem)
				buckets.append(temp)
				# print(temp)
		
			
			user = User_dto(user_data[0].UserProfileNickname, user_data[0].UserProfileImage)
			bucketlist = Bucketlist_dto(user_data[0].bucketListTitle, user_data[0].bucketListImage)
			
			temp = User_recoomm_dto(user, bucketlist, buckets)
			# print(temp)
			result.append(temp)

	if(len(result) != 0):
		logger.info(f"cache endpoint: {endpoint}")

		for i in result:
			rd.rpush(endpoint, json.dumps(i, default=lambda x: x.__dict__, ensure_ascii=False).encode('utf-8') )
		rd.expire(endpoint, 180)

		logger.info(f"response data size: {len(result[skip:limit])}")
	
	# data = {"content": result, "last": is_end, "size": size, "number": page, "empty": len(result) == 0}
	data = {"content": result[skip:limit], "last": limit >= len(result), "size": size, "number": page, "empty": len(result[skip:limit]) == 0}
	response = {"data": data, "success": True}
	
	logger.info(f"response size: {len(response['data'])}")
		
	return response


def social_random_recomm(db: Session, userSeq: int, size: int, page: int):
	rd = redis_config()
	
	skip = size*page
	limit = size*page+size

	endpoint = "social/" + str(userSeq) + "/random"

	cache_size = rd.llen(endpoint)
	if(cache_size != 0):
		logger.info(f"redis cache O: {endpoint}")
		response = get_response(endpoint, size, page, cache_size)
		return response

	user_list_data_temp = db.query(User.seq).filter(User.seq != userSeq).filter(User.is_delete == 0).all()

	logger.info(f"user list data: {user_list_data_temp}")

	user_list_data = []

	for i in user_list_data_temp:
		user_list_data.append(i.seq)
	
	# print(user_list_data)
	user_sum = db.query(User).count()

	if(size > user_sum): 
		size = user_sum -1

	logger.info(f"size: {size}")

	random_user = random.sample(user_list_data, len(user_list_data))

	logger.info(f"random user list: {random_user}")

	result = []

	for i in random_user:
		check = db.query(User).filter(User.seq == i).all()
		logger.info(f"check: {check}")
		if(check[0].nickname is None or check[0].seq == userSeq):
			logger.info("동일 사용자이거나 혹은 닉네임이 없는 사용자입니다.")
			continue

		user_data = db.query(BucketList.title.label('bucketListTitle'), BucketList.image.label('bucketListImage'), \
			User.profile_image.label('UserProfileImage'), User.nickname.label('UserProfileNickname'), \
			Category.item.label('CategoryItem'), \
			AddedBucket.emoji.label('BucketEmoji'), PublicBucket.title.label('BucketTitle'))\
		.filter(BucketListMember.bucketlist_seq == BucketList.seq)\
		.filter(BucketList.type == 'SINGLE')\
		.filter(BucketListMember.user_seq == i)\
		.filter(AddedBucket.bucketlist_seq == BucketList.seq)\
		.filter(AddedBucket.bucket_seq == PublicBucket.seq)\
		.filter(BucketListMember.user_seq == User.seq)\
		.filter(AddedBucket.is_delete == 0)\
		.filter(BucketList.is_public == 1)\
		.filter(PublicBucket.category_seq == Category.seq)\
		.all()

		logger.info(f"user data len: {len(user_data)}")

		if (len(user_data) != 0):
			buckets = []

			for j in user_data:
				temp = Bucket_dto(j.BucketTitle, j.BucketEmoji, j.CategoryItem)
				buckets.append(temp)			
				# print(temp)
			
			user = User_dto(user_data[0].UserProfileNickname, user_data[0].UserProfileImage)
			# logger.info(f"user: {user.__str__}")

			bucketlist = Bucketlist_dto(user_data[0].bucketListTitle, user_data[0].bucketListImage)
			# logger.info(f"bucketlist: {bucketlist.__str__}")
			
			temp = User_recoomm_dto(user, bucketlist, buckets)
			result.append(temp)

			# logger.info(f"result: {result.__str__}")

	logger.info(f"cache endpoint: {endpoint}")

	for i in result:
		rd.rpush(endpoint, json.dumps(i, default=lambda x: x.__dict__, ensure_ascii=False).encode('utf-8') )
	rd.expire(endpoint, 180)
	
	logger.info(f"response data size: {len(result[skip:limit])}")
	
	# data = {"content": result, "last": ie_end, "size": size, "number": page, "empty": len(result) == 0}
	data = {"content": result[skip:limit], "last": limit >= len(result), "size": size, "number": page, "empty": len(result[skip:limit]) == 0}
	response = {"data": data, "success": True}
		
	return response


def bucket_random_recomm(db: Session, userSeq: int, size: int, page: int, search_category_seq: int):

	rd = redis_config()

	skip = page*size
	limit = page*size+size

	endpoint = "buckets/" + str(userSeq) + "/" + "random/" + str(search_category_seq)

	cache_size = rd.llen(endpoint)
	if(cache_size != 0):
		logger.info(f"redis cache O: {endpoint}")
		response = get_response(endpoint, size, page, cache_size)
		return response

	if(search_category_seq == 0):
		pb_data = db.query(PublicBucket.emoji, PublicBucket.title, PublicBucket.added_count, PublicBucket.seq.label("bucket_seq"), Category.seq.label("category_seq"), Category.item)\
				.filter(PublicBucket.is_public == 1)\
				.filter(PublicBucket.category_seq == Category.seq)\
				.filter(PublicBucket.is_delete == 0)\
				.filter(PublicBucket.category_seq != 'null')\
				.all()
		
	else:
		pb_data = db.query(PublicBucket.emoji, PublicBucket.title, PublicBucket.added_count, PublicBucket.seq.label("bucket_seq"), Category.seq.label("category_seq"), Category.item)\
				.filter(PublicBucket.is_public == 1)\
				.filter(PublicBucket.category_seq == Category.seq)\
				.filter(PublicBucket.is_delete == 0)\
				.filter(PublicBucket.category_seq == search_category_seq)\
				.filter(PublicBucket.category_seq != 'null')\
				.all()


	prefer_data = db.query(PublicBucket.title, PublicBucket.category_seq)\
			.filter(Preference.user_seq == userSeq)\
			.filter(Preference.is_delete == 0)\
			.filter(Preference.bucket_seq == PublicBucket.seq)\
			.filter(PublicBucket.category_seq != 'null')\
			.all()
	
	list_prefer_data = []

	for i in prefer_data:
		list_prefer_data.append(i.title)

	random_pb_data= random.sample(pb_data, len(pb_data))

	temp_result = []

	for i in random_pb_data:
		# pb_data.remove(i)

		is_added = i.title in list_prefer_data
		category = Category_dto(i.category_seq, i.item)
		temp = Bucket_recoomm_dto(i.title, i.emoji, i.added_count, i.bucket_seq, is_added, category)
		temp_result.append(temp)

	
	logger.info(f"redis endpoint: {endpoint}")

	for i in temp_result:
		rd.rpush(endpoint, json.dumps(i, default=lambda x: x.__dict__, ensure_ascii=False).encode('utf-8') )
	rd.expire(endpoint, 180)

	logger.info(f"response data size: {len(temp_result[skip:limit])}")

	data = {"content": temp_result[skip:limit], "last": len(pb_data) < limit, "size": size, "number": page, "empty": len(temp_result) == 0}
	response = {"data": data, "success": True}

	return response


# redis 읽어오기
def get_response(endpoint, size, page, cache_size, *args):
	rd = redis_config()

	if(len(args) != 0):
		db = args[0]
		bucketlist = args[1]

		bucketlist_data = db.query(PublicBucket.title)\
		.filter(BucketList.seq == bucketlist)\
		.filter(BucketList.seq == AddedBucket.bucketlist_seq)\
		.filter(AddedBucket.bucket_seq == PublicBucket.seq)\
		.filter(AddedBucket.is_delete == 0)\
		.filter(PublicBucket.category_seq != 'null')\
		.all()

		list_prefer_data = []

		for i in bucketlist_data:
			list_prefer_data.append(i.title)


	skip = size*page
	limit = size*page+size-1

	logger.info(f"size, page, skip, limit: {size}, {page}, {skip}, {limit}")
	logger.info(f"cache size: {cache_size}")

	result = rd.lrange(endpoint, skip, limit)
	ret = []
	
	for r in result:
		temp = json.loads(r)
		if(len(args) != 0):
			temp['isAdded'] = temp['title'] in list_prefer_data
		ret.append(temp)

		
	data = {"content": ret, "last": limit+1 >= cache_size, "size": size, "number": page, "empty": len(ret) == 0}
	response = {"data": data, "success": True}

	return response


# def bucket_recommand_cf(prefer_data, pb_data, userSeq, page, size, search_category_seq):

# 	# json 형태로 변환
# 	prefer_data = jsonable_encoder(prefer_data)
# 	pb_data = jsonable_encoder(pb_data)

# 	# DataFrame 형태로 변환
# 	prefer_data = pd.DataFrame(prefer_data)
# 	pb_data = pd.DataFrame(pb_data)
# 	buckets = pb_data.set_index('bucket_seq')

# 	# is_delete 값을 rating으로 사용
# 	prefer_data['is_delete'] = prefer_data['is_delete']+1
# 	# if(prefer_data['is_delete'] == 1):
# 	# 	prefer_data['is_delete']+1

# 	# print(pb_data.head(3))
# 	# print(prefer_data.head(3))

# 	# reader = Reader(rating_scale=(0.5, 5.0))
# 	# temp = Dataset.load_from_df(prefer_data[['seq', 'bucket_seq', 'user_seq']], reader)
	
# 	userlen = len(prefer_data["user_seq"].unique())
# 	pblen = len(pb_data["seq"].unique())

# 	logger.info(f"고유 아이디 수: {userlen}")
# 	logger.info(f"공개된 버킷리스트 수: {pblen}")

# 	# index = user_seq, column = bucket_seq 행렬 만들기
# 	x = prefer_data.copy()
# 	y = prefer_data['user_seq']

# 	iteration = np.arange(0.20, 1.00, 0.01)

# 	global a
# 	a = "train_test_split"

# 	for i in iteration:
# 		try:
# 			logger.info(f"try test size: {round(i, 5)}")
# 			x_train, x_test, y_train, y_test = train_test_split(x, y, test_size=round(i, 5), stratify=y, random_state=0)
# 			logger.info(f"success {a}")
# 			a = "pivot"
# 			x_train = x_train.reset_index(drop=True)
# 			# test_size = 0.25, 25% 랜덤 데이터가 x_test로 추출됨
# 			prefer_matrix = x_train.pivot(values='is_delete', index='user_seq', columns='bucket_seq')
# 			logger.info(f"success {a}")
# 			break
# 		except:
# 			logger.info(f"fail {a}")
# 			a = "train_test_split"
# 			if(i >= 0.60):
# 				return "fail"
# 				# raise HTTPException(status_code=400, detail="too low data")
# 			pass

# 	# user sim matrix
# 	matrix_dummy = prefer_matrix.copy().fillna(0)
# 	user_sim = cosine_similarity(matrix_dummy, matrix_dummy)
# 	user_sim = pd.DataFrame(user_sim, index=prefer_matrix.index, columns=prefer_matrix.index)

# 	# temp = user_sim[['user_seq', userSeq]]
# 	# print(temp)


# 	def cf_simple(userSeq, bucketSeq):
# 		if(bucketSeq in prefer_matrix):
# 			sim_scores = user_sim[userSeq]
# 			bucket_ratings = prefer_matrix[bucketSeq]

# 			none_rating_idx = bucket_ratings[bucket_ratings.isnull()].index

# 			bucket_ratings = bucket_ratings.dropna()

# 			sim_scores = sim_scores.drop(none_rating_idx)

# 			mean_rating = np.dot(sim_scores, bucket_ratings) / sim_scores.sum()

# 		else:
# 			mean_rating = 0

# 		return mean_rating
	
# 	# RMSE 계산 함수
# 	def RMSE(y_true, y_pred):
# 			return np.sqrt(np.mean((np.array(y_true) - np.array(y_pred))**2))
	
	
# 	# score 함수 정의 : 모델을 입력값으로 받음 
# 	def score(model):
# 			id_pairs = zip(x_test['userSeq'], x_test['bucketSeq'])
# 			y_pred = np.array([model(user, bucket) for (user, bucket) in id_pairs])
# 			y_true = np.array(x_test['rating'])
# 			return RMSE(y_true, y_pred)
		
# 	# 정확도 계산
# 	score(cf_simple)

# 	def recommender(userSeq):
# 		rd = redis_config()

# 		skip = page*size
# 		limit = page*size+size

# 		predictions = []
# 		# 이미 담은 버킷의 인덱스 추출 -> 추천 시 제외해야 함 
# 		rated_index = prefer_matrix.loc[userSeq][prefer_matrix.loc[userSeq].notnull()].index
# 		# 해당 사용자가 담지 않은 버킷만 선택 
# 		items = prefer_matrix.loc[userSeq].drop(rated_index)
		
# 		# 예상평점 계산
# 		for item in items.index:
# 				predictions.append(cf_simple(userSeq, item))
																		
# 		recommendations = pd.Series(data=predictions, index=items.index, dtype=float)
# 		recommendations = recommendations.sort_values(ascending=False)       
# 		recommended_items = buckets.loc[recommendations.index]['title']

# 		endpoint = "buckets/" + str(userSeq) + "/" + "cf/" + str(search_category_seq)

# 		for i in recommended_items:
# 			rd.rpush(endpoint, json.dumps(i, default=lambda x: x.__dict__, ensure_ascii=False).encode('utf-8') )
# 		rd.expire(endpoint, 180)

# 		logger.info(f"response data size: {len(recommended_items[skip:limit])}")

# 		data = {"content": recommended_items[skip:limit], "last": len(pb_data) < limit, "size": size, "number": page, "empty": len(recommended_items) == 0}
# 		response = {"data": data, "success": True}

# 		return response

# 	recommender(userSeq)
