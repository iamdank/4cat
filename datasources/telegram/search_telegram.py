"""
Search Telegram via API
"""
import traceback
import datetime
import hashlib
import asyncio
import json
import time
import re

from pathlib import Path

from backend.abstract.search import Search
from common.lib.exceptions import QueryParametersException, ProcessorInterruptedException
from common.lib.helpers import convert_to_int, UserInput

from datetime import datetime
from telethon import TelegramClient
from telethon.errors.rpcerrorlist import UsernameInvalidError, TimeoutError
from telethon.tl.types import User, PeerChannel, PeerChat, PeerUser

import config


class SearchTelegram(Search):
    """
    Search Telegram via API
    """
    type = "telegram-search"  # job ID
    category = "Search"  # category
    title = "Telegram API search"  # title displayed in UI
    description = "Scrapes messages from open Telegram groups via its API."  # description displayed in UI
    extension = "ndjson"  # extension of result file, used internally and in UI
    is_local = False    # Whether this datasource is locally scraped
    is_static = False   # Whether this datasource is still updated

    # not available as a processor for existing datasets
    accepts = [None]

    # cache
    eventloop = None
    usermap = {}
    botmap = {}

    max_workers = 1
    max_retries = 3

    options = {
        "intro": {
            "type": UserInput.OPTION_INFO,
            "help": "Messages are scraped in reverse chronological order: the most recent message for a given entity "
                    "(e.g. a group) will be scraped first.\n\nTo query the Telegram API, you need to supply your [API "
                    "credentials](https://my.telegram.org/apps). 4CAT at this time does not support two-factor "
                    "authentication for Telegram."
        },
        "api_id": {
            "type": UserInput.OPTION_TEXT,
            "help": "API ID",
            "cache": True,
        },
        "api_hash": {
            "type": UserInput.OPTION_TEXT,
            "help": "API Hash",
            "cache": True,
        },
        "api_phone": {
            "type": UserInput.OPTION_TEXT,
            "help": "Phone number",
            "cache": True,
            "default": "+xxxxxxxxxx"
        },
        "security-code": {
            "type": UserInput.OPTION_TEXT,
            "help": "Security code",
            "sensitive": True
        },
        "divider": {
            "type": UserInput.OPTION_DIVIDER
        },
        "query-intro": {
            "type": UserInput.OPTION_INFO,
            "help": "You can scrape up to **25** items at a time. Separate the items with commas or line breaks."
        },
        "query": {
            "type": UserInput.OPTION_TEXT_LARGE,
            "help": "Entities to scrape",
            "tooltip": "Separate with commas or line breaks."
        },
        "max_posts": {
            "type": UserInput.OPTION_TEXT,
            "help": "Messages per group",
            "min": 1,
            "max": 50000,
            "default": 10
        },
        "daterange": {
            "type": UserInput.OPTION_DATERANGE,
            "help": "Date range"
        },
        "divider-2": {
            "type": UserInput.OPTION_DIVIDER
        },
        "info-sensitive": {
            "type": UserInput.OPTION_INFO,
            "help": "Your API credentials and phone number **will be sent to the 4CAT server** and will be stored "
                    "there while data is fetched. After the dataset has been created your credentials will be "
                    "deleted from the server, unless you enable the option below. If you want to download images "
                    "attached to the messages in your collected data, you need to enable this option. Your "
                    "credentials will never be visible to other users and can be erased later via the result page."
        },
        "save-sensitive": {
            "type": UserInput.OPTION_TOGGLE,
            "help": "Save session:",
            "default": False
        }
    }

    def get_items(self, query):
        """
        Execute a query; get messages for given parameters

        Basically a wrapper around execute_queries() to call it with asyncio.

        :param dict query:  Query parameters, as part of the DataSet object
        :return list:  Posts, sorted by thread and post ID, in ascending order
        """
        if "api_phone" not in query or "api_hash" not in query or "api_id" not in query:
            self.dataset.update_status("Could not create dataset since the Telegram API Hash and ID are missing. Try "
                                       "creating it again from scratch.", is_final=True)
            return None

        results = asyncio.run(self.execute_queries())

        if not query.get("save-sensitive"):
            self.dataset.delete_parameter("api_hash", instant=True)
            self.dataset.delete_parameter("api_phone", instant=True)
            self.dataset.delete_parameter("api_id", instant=True)
            
        return results

    async def execute_queries(self):
        """
        Get messages for queries

        This is basically what would be done in get_items(), except due to
        Telethon's architecture this needs to be called in an async method,
        which is this one.
        """
        # session file has been created earlier, and we can re-use it here in
        # order to avoid having to re-enter the security code
        query = self.parameters

        hash_base = query["api_phone"].replace("+", "") + query["api_id"] + query["api_hash"]
        session_id = hashlib.blake2b(hash_base.encode("ascii")).hexdigest()
        session_path = Path(config.PATH_ROOT).joinpath(config.PATH_SESSIONS, session_id + ".session")

        client = None

        def cancel_start():
            """
            Replace interactive phone number input in Telethon

            By default, if Telethon cannot use the given session file to
            authenticate, it will interactively prompt the user for a phone
            number on the command line. That is not useful here, so instead
            raise a RuntimeError. This will be caught below and the user will
            be told they need to re-authenticate via 4CAT.
            """
            raise RuntimeError("Connection cancelled")

        try:
            client = TelegramClient(str(session_path), int(query.get("api_id")), query.get("api_hash"),
                                    loop=self.eventloop)
            await client.start(phone=cancel_start)
        except RuntimeError:
            # session is no longer useable, delete file so user will be asked
            # for security code again
            self.dataset.update_status(
                "Session is not authenticated: login security code may have expired. You need to re-enter the security code.",
                is_final=True)

            if session_path.exists():
                session_path.unlink()

            if client and hasattr(client, "disconnect"):
                await client.disconnect()
            return []
        except Exception as e:
            self.dataset.update_status("Error connecting to the Telegram API with provided credentials.", is_final=True)
            if client and hasattr(client, "disconnect"):
                await client.disconnect()
            return []

        # ready our parameters
        parameters = self.dataset.get_parameters()
        queries = [query.strip() for query in parameters.get("query", "").split(",")]
        max_items = convert_to_int(parameters.get("items", 10), 10)
        
        # Telethon requires the offset date to be a datetime date
        max_date = parameters.get("max_date")
        if max_date:
            try:
                max_date = datetime.fromtimestamp(int(max_date))
            except ValueError:
                max_date = None
        
        # min_date can remain an integer
        min_date = parameters.get("min_date")
        if min_date:
            try:
                min_date = int(min_date)
            except ValueError:
                min_date = None

        posts = []
        try:
            async for post in self.gather_posts(client, queries, max_items, min_date, max_date):
                posts.append(post)
            print(posts)
            return posts
        except Exception as e:
            self.dataset.update_status("Error scraping posts from Telegram")
            self.log.error("Telegram scraping error: %s" % traceback.format_exc())
            return []
        finally:
            await client.disconnect()

    async def gather_posts(self, client, queries, max_items, min_date, max_date):
        """
        Gather messages for each entity for which messages are requested

        :param TelegramClient client:  Telegram Client
        :param list queries:  List of entities to query (as string)
        :param int max_items:  Messages to scrape per entity
        :param int min_date:  Datetime date to get posts after
        :param int max_date:  Datetime date to get posts before
        :return list:  List of messages, each message a dictionary.
        """

        for query in queries:
            delay = 10
            retries = 0

            while True:
                self.dataset.update_status("Fetching messages for entity '%s'" % query)
                i = 0
                try:
                    entity_posts = 0
                    async for message in client.iter_messages(entity=query, offset_date=max_date):
                        entity_posts += 1
                        i += 1
                        if self.interrupted:
                            raise ProcessorInterruptedException(
                                "Interrupted while fetching message data from the Telegram API")

                        if entity_posts % 100 == 0:
                            self.dataset.update_status(
                                "Retrieved %i posts for entity '%s' (%i total)" % (entity_posts, query, i))

                        if message.action is not None:
                            # e.g. someone joins the channel - not an actual message
                            continue

                        serialized_message = SearchTelegram.serialize_obj(message)

                        # Stop if we're below the min date
                        if min_date and serialized_message.get("date") < min_date:
                            break

                        yield serialized_message

                        if entity_posts > max_items:
                            break

                except (ValueError, UsernameInvalidError) as e:
                    self.dataset.update_status("Could not scrape entity '%s'" % query)

                except TimeoutError:
                    if retries < 3:
                        self.dataset.update_status(
                            "Tried to fetch messages for entity '%s' but timed out %i times. Skipping." % (
                            query, retries))
                        break

                    self.dataset.update_status(
                        "Got a timeout from Telegram while fetching messages for entity '%s'. Trying again in %i seconds." % (
                        query, delay))
                    time.sleep(delay)
                    delay *= 2
                    continue

                break

    def map_item(message):
        """
        Convert Message object to 4CAT-ready data object

        :param Message message:  Message to parse
        :param str entity:  Entity this message was imported from
        :return dict:  4CAT-compatible item object
        """
        thread = message["_chat"]["username"]

        # determine username
        # API responses only include the user *ID*, not the username, and to
        # complicate things further not everyone is a user and not everyone
        # has a username. If no username is available, try the first and
        # last name someone has supplied
        fullname = ""
        username = ""
        user_id = message.get("_sender", {}).get("id")
        user_is_bot = message.get("_sender", {}).get("bot", False)

        if message.get("_sender", {}).get("username"):
            username = message["_sender"]["username"]

        if message.get("_sender", {}).get("first_name"):
            fullname += message["_sender"]["first_name"]

        if message.get("_sender", {}).get("last_name"):
            fullname += " " + message["_sender"]["last_name"]

        fullname = fullname.strip()

        # determine media type
        # these store some extra information of the attachment in
        # attachment_data. Since the final result will be serialised as a csv
        # file, we can only store text content. As such some media data is
        # serialised as JSON.
        attachment_type = SearchTelegram.get_media_type(message["media"])
        if attachment_type == "contact":
            attachment = message["media"]["contact"]
            attachment_data = json.dumps({property: attachment.get(property) for property in
                                          ("phone_number", "first_name", "last_name", "vcard", "user_id")})

        elif attachment_type == "document":
            # videos, etc
            # This could add a separate routine for videos to make them a
            # separate type, which could then be scraped later, etc
            attachment_type = message["media"]["document"]["mime_type"].split("/")[0]
            if attachment_type == "video":
                attachment = message["media"]["document"]
                attachment_data = json.dumps({
                    "id": attachment["id"],
                    "dc_id": attachment["dc_id"],
                    "file_reference": attachment["file_reference"],
                })
            else:
                attachment_data = ""

        elif attachment_type in ("geo", "geo_live"):
            # untested whether geo_live is significantly different from geo
            attachment_data = "%s %s" % (message["geo"]["lat"], message["geo"]["long"])

        elif attachment_type == "photo":
            # we don't actually store any metadata about the photo, since very
            # little of the metadata attached is of interest. Instead, the
            # actual photos may be downloaded via a processor that is run on the
            # search results
            attachment = message["media"]["photo"]
            attachment_data = json.dumps({
                "id": attachment["id"],
                "dc_id": attachment["dc_id"],
                "file_reference": attachment["file_reference"],
            })

        elif attachment_type == "poll":
            # unfortunately poll results are only available when someone has
            # actually voted on the poll - that will usually not be the case,
            # so we store -1 as the vote count
            attachment = message["media"]["poll"]
            options = {option.option: option.text for option in attachment.poll.answers}
            attachment_data = json.dumps({
                "question": attachment.poll.question,
                "voters": attachment.results.total_voters,
                "answers": [{
                    "answer": options[answer.option],
                    "votes": answer.voters
                } for answer in attachment.results.results] if attachment.results.results else [{
                    "answer": options[option],
                    "votes": -1
                } for option in options]
            })

        elif attachment_type == "url":
            # easy!
            attachment_data = message["media"].get("web_preview", {}).get("url", "")

        else:
            attachment_data = ""

        # was the message forwarded from somewhere and if so when?
        forwarded = ""
        forwarded_timestamp = ""
        if message["fwd_from"]:
            forwarded_timestamp = int(message["fwd_from"]["date"])

            if message["fwd_from"].get("post_author"):
                forwarded = message["fwd_from"].get("post_author")
            elif message["fwd_from"].get("from_id"):
                forwarded = message["fwd_from"].get("from_id").get("user_id", "")
            elif message["fwd_from"].get("channel_id"):
                forwarded = message["fwd_from"].get("channel_id")

        msg = {
            "id": message["id"],
            "thread_id": thread,
            "chat": message["_chat"]["username"],
            "author": user_id,
            "author_username": username,
            "author_name": fullname,
            "author_is_bot": user_is_bot,
            "author_forwarded_from": forwarded if forwarded else "",
            "subject": "",
            "body": message["message"],
            "reply_to": message.get("reply_to_msg_id", ""),
            "views": message["views"] if message["views"] else "",
            "timestamp": datetime.fromtimestamp(message["date"]).strftime("%Y-%m-%d %H:%M:%S"),
            "unix_timestamp": int(message["date"]),
            "timestamp_edited": datetime.fromtimestamp(message["edit_date"]).strftime("%Y-%m-%d %H:%M:%S") if message["edit_date"] else "",
            "unix_timestamp_edited": int(message["edit_date"]) if message["edit_date"] else "",
            "timestamp_forwarded_from": datetime.fromtimestamp(forwarded_timestamp).strftime("%Y-%m-%d %H:%M:%S") if forwarded_timestamp else "",
            "unix_timestamp_forwarded_from": forwarded_timestamp,
            "attachment_type": attachment_type,
            "attachment_data": attachment_data
        }

        return msg

    @staticmethod
    def get_media_type(media):
        """
        Get media type for a Telegram attachment

        :param media:  Media object
        :return str:  Textual identifier of the media type
        """
        try:
            return {
                "NoneType": "",
                "MessageMediaContact": "contact",
                "MessageMediaDocument": "document",
                "MessageMediaEmpty": "",
                "MessageMediaGame": "game",
                "MessageMediaGeo": "geo",
                "MessageMediaGeoLive": "geo_live",
                "MessageMediaInvoice": "invoice",
                "MessageMediaPhoto": "photo",
                "MessageMediaPoll": "poll",
                "MessageMediaUnsupported": "unsupported",
                "MessageMediaVenue": "venue",
                "MessageMediaWebPage": "url"
            }[media.get("_type", None)]
        except (AttributeError, KeyError):
            return ""

    @staticmethod
    def serialize_obj(input_obj):
        """
        Serialize an object as a dictionary

        Telethon message objects are not serializable by themselves, but most
        relevant attributes are simply struct classes. This function replaces
        those that are not with placeholders and then returns a dictionary that
        can be serialized as JSON.

        :param obj:  Object to serialize
        :return:  Serialized object
        """
        scalars = (int, str, float, list, tuple, set, dict, bool)

        if type(input_obj) in scalars or input_obj is None:
            return input_obj

        if type(input_obj) is not dict:
            obj = input_obj.__dict__
        else:
            obj = input_obj.copy()

        mapped_obj = {}
        for item, value in obj.items():
            if type(value) is datetime:
                mapped_obj[item] = value.timestamp()
            elif type(value).__module__ in ("telethon.tl.types", "telethon.tl.custom.forward"):
                mapped_obj[item] = SearchTelegram.serialize_obj(value)
                if type(obj[item]) is not dict:
                    mapped_obj[item]["_type"] = type(value).__name__
            elif type(value) is list:
                mapped_obj[item] = [SearchTelegram.serialize_obj(item) for item in value]
            elif type(value).__module__[0:8] == "telethon":
                # some type of internal telethon struct
                print(type(value).__module__)
                continue
            elif type(value) is bytes:
                mapped_obj[item] = value.hex()
            elif type(value) not in scalars and value is not None:
                # type we can't make sense of here
                print(value)
                continue
            else:
                mapped_obj[item] = value

        return mapped_obj

    @staticmethod
    def validate_query(query, request, user):
        """
        Validate Telegram query

        :param dict query:  Query parameters, from client-side.
        :param request:  Flask request
        :param User user:  User object of user who has submitted the query
        :return dict:  Safe query parameters
        """
        # no query 4 u
        if not query.get("query", "").strip():
            raise QueryParametersException("You must provide a search query.")

        if not query.get("api_id", None) or not query.get("api_hash", None) or not query.get("api_phone", None):
            raise QueryParametersException("You need to provide valid Telegram API credentials first.")

        # reformat queries to be a comma-separated list with no wrapping
        # whitespace
        whitespace = re.compile(r"\s+")
        items = whitespace.sub("", query.get("query").replace("\n", ","))
        if len(items.split(",")) > 25:
            raise QueryParametersException("You cannot query more than 25 items at a time.")

        # eliminate empty queries
        items = ",".join([item for item in items.split(",") if item])

        # the dates need to make sense as a range to search within
        min_date, max_date = query.get("daterange")

        # simple!
        return {
            "items": query.get("max_posts"),
            "query": items,
            "board": "",  # needed for web interface
            "api_id": query.get("api_id"),
            "api_hash": query.get("api_hash"),
            "api_phone": query.get("api_phone"),
            "save-sensitive": query.get("save-sensitive"),
            "min_date": min_date,
            "max_date": max_date
        }

    @classmethod
    def get_options(cls=None, parent_dataset=None, user=None):
        """
        Get processor options

        This method by default returns the class's "options" attribute, but
        will lift the limit on the amount of messages scraped per group if the
        user requesting the options has been configured as such.

        :param DataSet parent_dataset:  An object representing the dataset that
        the processor would be run on
        :param User user:  Flask user the options will be displayed for, in
        case they are requested for display in the 4CAT web interface. This can
        be used to show some options only to privileges users.
        """
        options = cls.options.copy()

        if user and user.get_value("telegram.can_query_all_messages", False) and "max" in options["max_posts"]:
            del options["max_posts"]["max"]

        return options
