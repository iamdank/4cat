""" 4CAT configuration """
import os
import json
import psycopg2
import psycopg2.extras
import configparser

class ConfigManager:
    """
    Manage 4CAT configuation options that cannot be recorded in database
    (generally because they are used to get to the database!).

    Note: some options are here until additional changes are made and they can
    be moved to more appropriate locations.
    """
    CONFIG_FILE = 'backend/config_defaults.ini'
    DOCKER_CONFIG_FILE = 'docker/shared/docker_config.ini'

    #TODO: work out best structure for docker vs non-docker
    # Do not need two configs, BUT Docker config.ini has to be in shared volume for both front and backend to access it
    config_reader = configparser.ConfigParser()
    USING_DOCKER = False
    if os.path.exists(DOCKER_CONFIG_FILE):
        config_reader.read(DOCKER_CONFIG_FILE)
        if config_reader['DOCKER'].getboolean('use_docker_config'):
            # User docker_config.ini
            USING_DOCKER = True
            pass
        elif os.path.exists(CONFIG_FILE):
            # if docker not enabled, use default config
            config_reader.read(CONFIG_FILE)

    DB_HOST = config_reader['DATABASE'].get('db_host')
    DB_PORT = config_reader['DATABASE'].getint('db_port')
    DB_USER = config_reader['DATABASE'].get('db_user')
    DB_NAME = config_reader['DATABASE'].get('db_name')
    DB_PASSWORD = config_reader['DATABASE'].get('db_password')

    API_HOST = config_reader['API'].get('api_host')
    API_PORT = config_reader['API'].getint('api_port')

    PATH_ROOT = os.path.abspath(os.path.dirname(__file__))  # better don't change this
    PATH_LOGS = config_reader['PATHS'].get('path_logs', "")
    PATH_IMAGES = config_reader['PATHS'].get('path_images', "")
    PATH_DATA = config_reader['PATHS'].get('path_data', "")
    PATH_LOCKFILE = config_reader['PATHS'].get('path_lockfile', "")
    PATH_SESSIONS = config_reader['PATHS'].get('path_sessions', "")

    ANONYMISATION_SALT = config_reader['GENERATE'].get('anonymisation_salt')

    # Web tool settings
    # can be your server url or ip
    your_server = config_reader['SERVER'].get('server_name', 'localhost')
    SECRET_KEY = config_reader['GENERATE'].get('secret_key')
    if config_reader['SERVER'].getint('public_port') == 80:
      SERVER_NAME = your_server
    else:
      SERVER_NAME = f"{your_server}:{config_reader['SERVER'].get('public_port')}"
    HOSTNAME_WHITELIST = ["localhost", your_server]  # only these may access the web tool; "*" or an empty list matches everything
    HOSTNAME_WHITELIST_API = ["localhost", your_server]  # hostnames matching these are exempt from rate limiting

    #TODO: any datasource options need to be moved and modified into their search class
    # Data source configuration
    DATASOURCES = {
      "bitchute": {},
      "custom": {},
      "douban": {},
      "customimport": {},
      "parler": {},
      "reddit": {
    							"boards": "*",
    		},
      "telegram": {},
      "twitterv2": {"id_lookup": False}
    }

    #TODO: These Need to be moved to the Processors
    #####################
    # Processor Options #
    #####################

    # download_images.py
    MAX_NUMBER_IMAGES = 1000

    # YouTube variables to use for processors
    YOUTUBE_API_SERVICE_NAME = "youtube"
    YOUTUBE_API_VERSION = "v3"
    YOUTUBE_DEVELOPER_KEY = ""

    # Tumblr API keys to use for data capturing
    TUMBLR_CONSUMER_KEY = ""
    TUMBLR_CONSUMER_SECRET_KEY = ""
    TUMBLR_API_KEY = ""
    TUMBLR_API_SECRET_KEY = ""

    # Reddit API keys
    REDDIT_API_CLIENTID = ""
    REDDIT_API_SECRET = ""

    # tcat_auto_upload.py
    TCAT_SERVER = ''
    TCAT_TOKEN = ''
    TCAT_USERNAME = ''
    TCAT_PASSWORD = ''

    # pix-plot.py
    # If you host a version of https://github.com/digitalmethodsinitiative/dmi_pix_plot, you can use a processor to publish
    # downloaded images into a PixPlot there
    PIXPLOT_SERVER = ""

def get(attribute_name, default=None):
    """
    Checks if attribute defined in namespace and returns, if not then checks database for attribute and returns value
    """
    if attribute_name in dir(ConfigManager):
        # an explicitly defined attribute should always be called in favour
        # of this passthrough
        attribute = getattr(ConfigManager, attribute_name)
        return attribute
    else:
        try:
            connection = psycopg2.connect(dbname=ConfigManager.DB_NAME, user=ConfigManager.DB_USER, password=ConfigManager.DB_PASSWORD, host=ConfigManager.DB_HOST, port=ConfigManager.DB_PORT)
            cursor = connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            query = 'SELECT value FROM fourcat_settings WHERE name = %s'
            cursor.execute(query, (attribute_name, ))
            row = cursor.fetchone()
            connection.close()
            value = json.loads(row['value'])
        except (Exception, psycopg2.DatabaseError) as error:
            # TODO: log?
            print('Problem with attribute: ' + str(attribute_name) + ': ' + str(error))
            print('Connection: ' + str(connection))
            print('SQL row: ' + str(row))
            value = None
        finally:
            if connection is not None:
                connection.close()

        if value is None:
            return default
        else:
            return value

def set_value(attribute_name, value):
    """
    Updates database attribute with new value. Returns number of updated rows
    (which ought to be either 1 for success or 0 for failure).
    """
    # Check value is valid JSON
    try:
        value = json.dumps(value)
    except ValueError:
        return None

    try:
        connection = psycopg2.connect(dbname=ConfigManager.DB_NAME, user=ConfigManager.DB_USER, password=ConfigManager.DB_PASSWORD, host=ConfigManager.DB_HOST, port=ConfigManager.DB_PORT)
        cursor = connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        query = 'UPDATE fourcat_settings SET value = %s WHERE name = %s'
        cursor.execute(query, (value, attribute_name))
        updated_rows = cursor.rowcount
        connection.commit()
        connection.close()
    except (Exception, psycopg2.DatabaseError) as error:
        # TODO: log?
        print(error)
        updated_rows = None
    finally:
        if connection is not None:
            connection.close()

    return updated_rows

def get_all():
    """
    Gets all database settings in 4cat_settings table. These are editable, while
    other attributes (part of the ConfigManager class are not directly editable)
    """
    try:
        connection = psycopg2.connect(dbname=ConfigManager.DB_NAME, user=ConfigManager.DB_USER, password=ConfigManager.DB_PASSWORD, host=ConfigManager.DB_HOST, port=ConfigManager.DB_PORT)
        cursor = connection.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        query = 'SELECT name, value FROM fourcat_settings'
        cursor.execute(query)
        rows = cursor.fetchall()
        connection.close()
        values = {row['name']:row['value'] for row in rows}
    except (Exception, psycopg2.DatabaseError) as error:
        # TODO: log?
        print(error)
        values = None
    finally:
        if connection is not None:
            connection.close()

    return values

# Web tool settings
# This is a pass through class; may not be the best way to do this
class FlaskConfig:
    FLASK_APP = get('FLASK_APP')
    SECRET_KEY = get('SECRET_KEY')
    SERVER_NAME = get('SERVER_NAME') # if using a port other than 80, change to localhost:specific_port
    SERVER_HTTPS = get('SERVER_HTTPS')  # set to true to make 4CAT use "https" in absolute URLs
    HOSTNAME_WHITELIST = get('HOSTNAME_WHITELIST')  # only these may access the web tool; "*" or an empty list matches everything
    HOSTNAME_WHITELIST_API = get('HOSTNAME_WHITELIST_API')  # hostnames matching these are exempt from rate limiting
    HOSTNAME_WHITELIST_NAME = get('HOSTNAME_WHITELIST_NAME')
