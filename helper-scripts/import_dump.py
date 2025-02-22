"""
Import 4chan data from Archived.moe csv dumps.

Several of these are downloadable here: https://archive.org/details/archivedmoe_db_201908.

For /v/, make sure to download this one: https://archive.org/download/archivedmoe_db_201908/v.csv.bz2

"""

import argparse
import json
import time
import csv
import sys
import os
import re

from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)) + "/..")
from common.lib.database import Database
from common.lib.logger import Logger

# parse parameters
cli = argparse.ArgumentParser()
cli.add_argument("-i", "--input", required=True, help="File to read from, containing a CSV dump")
cli.add_argument("-d", "--datasource", type=str, required=True, help="Datasource ID")
cli.add_argument("-b", "--board", type=str, required=True, help="Board name")
cli.add_argument("-s", "--skip_duplicates", type=str, required=True, help="If duplicate posts should be skipped (useful if there's already data in the table)")
cli.add_argument("-o", "--offset", type=int, required=False, help="How many rows to skip")
args = cli.parse_args()

if not Path(args.input).exists() or not Path(args.input).is_file():
	print("%s is not a valid folder name." % args.input)
	sys.exit(1)

logger = Logger()
db = Database(logger=logger, appname="queue-dump")

csvnone = re.compile(r"^N$")

safe = False
if args.skip_duplicates.lower() == "true":
	print("Skipping duplicate rows (ON CONFLICT DO NOTHING).")
	safe = True

with open(args.input, encoding="utf-8") as inputfile:

	if args.board == "v":
		# The /v/ dump has no headers and slightly different column ordering.
		# The unknown ones are irrelevant but also tricky to extract (e.g.
		# poster_country), so we're just calling them 'unknown_x'
		fieldnames = ("num", "subnum", "thread_num", "op", "timestamp", "timestamp_expired", "preview_orig", "preview_w", "preview_h", "media_filename", "media_w", "media_h", "media_size", "media_hash", "media_orig", "spoiler", "deleted", "author_type_id", "email", "name", "trip", "title", "comment", "sticky", "locked", "unknown_8", "unknown_9", "unknown_10", "unknown_11", "unknown_12", "unknown_13", "unknown_14")
	else:
		fieldnames = ("num", "subnum", "thread_num", "op", "timestamp", "timestamp_expired", "preview_orig", "preview_w", "preview_h", "media_filename", "media_w", "media_h", "media_size", "media_hash", "media_orig", "spoiler", "deleted", "capcode", "email", "name", "trip", "title", "comment", "sticky", "locked", "poster_hash", "poster_country", "exif")

	reader = csv.DictReader(inputfile, fieldnames=fieldnames, doublequote=False, escapechar="\\", strict=True)
	
	# Skip header
	next(reader, None)

	posts = 0
	posts_added = 0
	threads = {}
	threads_last_seen = {}

	# Show status
	if args.offset:
		print("Skipping %s rows." % args.offset)

	for post in reader:
		
		posts += 1

		# Skip rows if needed. Can be useful when importing didn't go correctly.
		if args.offset and posts < args.offset:
			continue
		
		post = {k: csvnone.sub("", post[k]) if post[k] else None for k in post}

		# We collect thread data first, even though we might skip this post
		if post["thread_num"] not in threads:
			threads[post["thread_num"]] = {
				"id": post["thread_num"],
				"board": args.board,
				"timestamp": 0,
				"timestamp_scraped": int(time.time()),
				"timestamp_modified": 0,
				"num_unique_ips": -1,
				"num_images": 0,
				"num_replies": 0,
				"limit_bump": False,
				"limit_image": False,
				"is_sticky": False,
				"is_closed": False,
				"post_last": 0
			}
		
		if post["op"] == "1":
			threads[post["thread_num"]]["timestamp"] = post["timestamp"]
			threads[post["thread_num"]]["is_sticky"] = post["sticky"] == "1"
			threads[post["thread_num"]]["is_closed"] = post["locked"] == "1"

		if post["media_filename"]:
			threads[post["thread_num"]]["num_images"] += 1

		threads[post["thread_num"]]["num_replies"] += 1
		threads[post["thread_num"]]["post_last"] = post["num"]
		threads[post["thread_num"]]["timestamp_modified"] = post["timestamp"]

		# We reset the count of when we last seen this thread to 1
		# to prevent committing incomplete thread data.
		# Increase the count for the other threads.
		threads_last_seen[post["thread_num"]] = 0
		for k, v in threads_last_seen.items():
			threads_last_seen[k] += 1
		
		if post["media_filename"] and len({"media_w", "media_h", "preview_h", "preview_w"} - set(post.keys())) == 0:
			dimensions = {"w": post["media_w"], "h": post["media_h"], "tw": post["preview_w"], "th": post["preview_h"]}
		else:
			dimensions = {}

		if post["subnum"] != "0":
			# ghost post
			continue

		post_data = {
			"id": post["num"],
			"board": args.board,
			"thread_id": post["thread_num"],
			"timestamp": post["timestamp"],
			"subject": post.get("title", ""),
			"body": post.get("comment", ""),
			"author": post.get("name", ""),
			"author_trip": post.get("trip", ""),
			"author_type": post.get("author_type", ""),
			"author_type_id": post["author_type_id"] if post["author_type_id"] != "" else "N",
			"country_name": "",
			"country_code": post.get("poster_country", ""),
			"image_file": post["media_filename"],
			"image_4chan": post["media_orig"],
			"image_md5": post.get("media_hash", ""),
			"image_filesize": post.get("media_size", 0),
			"image_dimensions": json.dumps(dimensions)
		}

		post_data = {k: str(v).replace("\x00", "") for k, v in post_data.items()}
		
		if post["deleted"] == "0":
			new_post = db.insert("posts_4chan", post_data, commit=False, safe=safe)
		
		else:
			
			# database.py throws a TypeError when the post already exists and we're not updating, since it didn't return a row. However, we need the id_seq field for deleted posts. In this case, catch the TypeError and simply retrieve the id_seq from the database.
			try:
				new_id = db.insert("posts_4chan", post_data, commit=False, safe=safe, return_field="id_seq")
			except TypeError:
				
				r = db.fetchone("SELECT id_seq FROM posts_4chan WHERE board = '%s' AND id = %i;" % (args.board, int(post["num"])))
				new_id = r["id_seq"] if r else None
			if new_id:
				new_post = db.insert("posts_4chan_deleted", {"id_seq": new_id, "timestamp_deleted": post["timestamp"]}, safe=True)

		posts_added += new_post
		
		# Insert per every 10000 posts
		if posts > 0 and posts % 10000 == 0:
			print("Committing posts %i - %i. %i new posts added. " % (posts - 10000, posts, posts_added), end="")

			# We're commiting the threads we didn't encounter in the last 100.000 posts. We're assuming they're complete and won't be seen in this archive anymore.
			# This is semi-necessary to prevent RAM hogging.
			threads_committed = 0
			for thread_seen, last_seen in threads_last_seen.items():
				if last_seen > 10000:
					db.upsert("threads_4chan", data=threads[thread_seen], commit=False, constraints=["id", "board"])
					threads.pop(thread_seen)
					threads_committed += 1

			# Remove committed threads from the last seen list
			threads_last_seen = {k: v for k, v in threads_last_seen.items() if v < 10000}

			print("Comitting %i threads (%i still updating)." % (threads_committed, len(threads)))
			
			db.commit()

	# Add the last threads as well
	print("Comitting leftover threads")
	for thread in threads.values():
		db.upsert("threads_4chan", data=thread, commit=False, constraints=["id", "board"])

	db.commit()

print("Done")

