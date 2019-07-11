"""
Generate network of wikipedia pages + categories in posts
"""
import requests
import csv
import re

from lxml import etree
from lxml.cssselect import CSSSelector as css
from io import StringIO

from backend.abstract.processor import BasicProcessor


class WikipediaCatgegoryNetwork(BasicProcessor):
	"""
	Generate Wikipedia network
	"""
	type = "wiki-category-network"  # job type ID
	category = "Networks"  # category
	title = "Wikipedia category network"  # title displayed in UI
	description = "Create a Gephi-compatible network comprised of wikipedia pages linked in the data set, linked to the categories they are part of. English Wikipedia only."  # description displayed in UI
	extension = "gdf"  # extension of result file, used internally and in UI

	def process(self):
		"""
		This takes a 4CAT results file as input, and outputs a new CSV file
		with all posts containing the original query exactly, ignoring any
		* or " in the query
		"""
		months = {}

		# we use these to extract URLs and host names if needed
		link_regex = re.compile(r"https?://en.wikipedia\.org/wiki/[^\s.]+")
		category_regex = re.compile(r"\[\[Category:[^\]]+\]\]")
		trailing_comma = re.compile(r",$")

		# initialise
		links = {}
		all_categories = {}
		counter = 1
		errors = 0
		page_categories = {}

		# find all links in post bodies
		self.dataset.update_status("Reading source file")
		with self.source_file.open() as input:
			reader = csv.DictReader(input)

			for post in reader:
				wiki_links = link_regex.findall(post["body"])
				wiki_links = [trailing_comma.sub("", link) for link in wiki_links]

				for link in wiki_links:
					link = "/wiki/".join(link.split("/wiki/")[1:]).split("#")[0]
					if link not in links:
						links[link] = 0

					links[link] += 1

		# just a helper function to get the HTML content of a node
		def stringify_children(node):
			from lxml.etree import tostring
			from itertools import chain
			parts = ([node.text] +
					 list(chain(*([c.text, tostring(c), c.tail] for c in node.getchildren()))) +
					 [node.tail])
			# filter removes possible Nones in texts and tails
			return ''.join(filter(None, parts))

		self.dataset.update_status("Fetching categories from Wikipedia API...")
		for link in links:
			if link not in page_categories:
				page_categories[link] = set()
				self.dataset.update_status(
					"Fetching categories from Wikipedia API (page %i of %i)" % (counter, len(links)))

				# fetch wikipedia source
				url = "https://en.wikipedia.org/w/index.php?title=" + link + "&action=edit"
				page = requests.get(url)

				if page.status_code != 200:
					errors += 1
					continue

				# get link to image file from HTML returned
				parser = etree.HTMLParser()
				tree = etree.parse(StringIO(page.content.decode("utf-8")), parser)

				try:
					wiki_source = stringify_children(css("#wpTextbox1")(tree)[0])
				except IndexError:
					# not a source page?
					errors += 1
					continue

				# extract category names from category link syntax
				categories = category_regex.findall(wiki_source)
				categories = set([":".join(category.split(":")[1:])[:-2].split("|")[0] for category in categories])

				# save category links
				for category in categories:
					if category not in all_categories:
						all_categories[category] = 0

					all_categories[category] += 1
					page_categories[link].add(category)

		# write GDF file
		with self.dataset.get_results_path().open("w", encoding="utf-8") as results:
			results.write("nodedef>name VARCHAR,type VARCHAR,weight INTEGER\n")
			for page in page_categories:
				results.write("'" + page.replace("_", " ") + "',page," + str(links[page]) + "\n")

			for category in all_categories:
				results.write("'" + category.replace("_", " ") + "',category," + str(all_categories[category]) + "\n")

			results.write("edgedef>node1 VARCHAR, node2 VARCHAR, weight INTEGER\n")
			for page in page_categories:
				for category in page_categories[page]:
					results.write("'" + page.replace("_", " ") + "','" + category.replace("_", " ") + "'\n")

		self.dataset.finish(len(page_categories))
