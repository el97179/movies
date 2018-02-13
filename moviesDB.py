#!/usr/bin/env python3

import sys
import json
import requests
import argparse
import webbrowser
import os

key = "d458f62"
n = 5 # max number of movies retrieved
title = []

while True:
	if (len(sys.argv)==1):
		title_str = input("search for a movie title: ")
		if ((title_str=="") or (title_str=="q") or (title_str=="Q")):
			break
		title = title_str.split(" ")
	else:
		for i in range(1, len(sys.argv)):
			title.append(sys.argv[i])

	url = "http://www.omdbapi.com/?apikey=" + key + "&s=" + "+".join(title)
	res = requests.get(url)
	if(res.ok):
		data = json.loads(res.content.decode("utf-8"))
		if (data["Response"]=="False"):
			print("No results found :(")
			if (len(sys.argv)>1):
				break
			else:
				continue
			
		results = data["Search"]
		top_n = min(n, int(data["totalResults"]))
		print("(top " + str(top_n) + " matching movies)")
		for i in range(0, top_n):
			print("\n#" + str(i+1) + ": " + results[i]["Title"] + " (" + results[i]["Year"] + ")")
			imdb_id = results[i]["imdbID"]
			url = "http://www.omdbapi.com/?apikey=" + key + "&i=" + imdb_id + "&plot=full"
			res = requests.get(url)
			film_data = json.loads(res.content.decode("utf-8"))
			if (film_data["Response"]=="False"):
				print("No results found for movie with imdb id: " + imdb_id)
				continue
			print("Director: " + film_data["Director"])
			print("Actors: " + film_data["Actors"])
			print("Country: " + film_data["Country"])
			print("Language: " + film_data["Language"])
			print("Genre: " + film_data["Genre"])
			print("Runtime: " + film_data["Runtime"])
			print("Plot: " + film_data["Plot"])
			print("Awards: " + film_data["Awards"])
			if (len(film_data["Ratings"])==0):
				print("Ratings: N/A")
			else:
				print("Ratings:")
				for j in range(0, len(film_data["Ratings"])):
					print("\t" + film_data["Ratings"][j]["Source"] + ": " + film_data["Ratings"][j]["Value"])
			if (i==0):
				html_dir = os.path.expanduser('~') + "/Videos/moviesDB/"
				os.makedirs(html_dir, exist_ok=True)
				html_url =  html_dir + results[i]["Title"] + ".html"
				html_file = open(html_url, "w")
				content = """<!DOCTYPE html>
							<html>
							<body>
							<h1>""" + results[i]["Title"] + " (" + results[i]["Year"] + """)</h1>
							<hr>
							<img src=\"""" + film_data["Poster"] + """\" style="float:left;margin-right:20px;width:400px">
							<p><b>Director:</b> """ + film_data["Director"] + """</p>
							<p><b>Actors:</b> """ + film_data["Actors"] + """</p>
							<p><b>Country:</b> """ + film_data["Country"] + """</p>
							<p><b>Language:</b> """ + film_data["Language"] + """</p>
							<p><b>Genre:</b> """ + film_data["Genre"] + """</p>
							<p><b>Runtime:</b> """ + film_data["Runtime"] + """</p>
							<p><b>Plot:</b> """ + film_data["Plot"] + """</p>
							<p><b>Awards:</b> """ + film_data["Awards"] + """</p>"""
				
				if (len(film_data["Ratings"])==0):
					content += "<p><b>Ratings:</b> N/A </p>"
				else:
					content += "<p><b>Ratings:</b></p>"
					content += "<ul>"
					for j in range(0, len(film_data["Ratings"])):
						if (film_data["Ratings"][j]["Source"] == "Internet Movie Database"):
							link = "\"http://www.imdb.com/title/" + imdb_id + "\""
						elif (film_data["Ratings"][j]["Source"] == "Rotten Tomatoes"):
							link = "\"https://www.rottentomatoes.com/search/?search=" + results[i]["Title"] + "\""
						elif (film_data["Ratings"][j]["Source"] == "Metacritic"):
							link = "\"http://www.metacritic.com/search/all/" + results[i]["Title"] + "/results\""
						content += """<li><a href=""" + link + """>""" + film_data["Ratings"][j]["Source"] + """: """ + film_data["Ratings"][j]["Value"] + """</li>"""
					content += "</ul>"
				content += """</body>
							</html>"""

				
				html_file.write(content)
				html_file.close()
				
				webbrowser.open(html_url)
		print("\n")
		
		if (len(sys.argv)>1):
			break
	
	else:
		res.raise_for_status()
		break
