"""
A module implementing a bot to notify editors when articles they create or
expand are nominated for DYK by someone else.
"""
import argparse
import ConfigParser
from datetime import datetime, timedelta
import functools
import json
import operator
import os.path
# pylint: disable=import-error
import pywikibot
import pywikibot.pagegenerators as pagegenerators
import re
import sys
import time
import traceback

from bs4 import BeautifulSoup
from clint.textui import prompt

CONFIG = None
BAD_TEXT = re.compile(r"(Self(-|\s)nominated|Category:((f|F)ailed|(p|P)assed) DYK)",
                      re.I)

def main():
    "The main function."
    print("Starting dyknotifier at " + datetime.utcnow().isoformat())
    read_config()
    verify_data_present()
    args = parse_args()
    wiki = pywikibot.Site("en", "wikipedia")
    wiki.login()
    people_to_notify = get_people_to_notify(wiki)
    people_to_notify = prune_list_of_people(people_to_notify)
    notify_people(people_to_notify, args, wiki)

def read_config():
    """Read the config file."""
    global CONFIG
    CONFIG = ConfigParser.RawConfigParser()
    CONFIG.read("/data/project/apersonbot/bot/dyknotifier/config.txt")

def verify_data_present():
    """Check that the already-notified database is there."""
    if not os.path.isfile(CONFIG.get("dyknotifier", "ALREADY_NOTIFIED_FILE")):
        print("Couldn't locate %s" % CONFIG.get("dyknotifier", "ALREADY_NOTIFIED_FILE"))
        sys.exit(1)

def parse_args():
    "Parse the arguments."
    parser = argparse.ArgumentParser(prog="DYKNotifier",
                                     description=\
                                     "Notify editors of their DYK noms.")
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="Confirm before each edit.")
    parser.add_argument("-c", "--count", type=int,
                        help="Notify at most n people.")
    return parser.parse_args()

def get_people_to_notify(wiki):
    """
    Returns a dict of user talkpages to notify about their creations and
    the noms about which they should be notified.
    """
    people_to_notify = dict()
    cat_dykn = pywikibot.Category(wiki, "Category:Pending DYK nominations")
    print("Getting nominations from " + cat_dykn.title() + "...")
    for nomination in pagegenerators.CategorizedPageGenerator(
            cat_dykn, content=True):
        wikitext = nomination.get()
        if not BAD_TEXT.search(wikitext):
            who_to_nominate = get_who_to_nominate(wikitext,
                                                  nomination.title())
            for username, nomination in who_to_nominate.items():
                people_to_notify[username] = people_to_notify.get(
                    username, []) + [nomination]

    print("Found {} people to notify.".format(len(people_to_notify)))
    return people_to_notify

# pylint: disable=too-many-branches
# pylint: disable=too-many-locals
def prune_list_of_people(people_to_notify):
    "Removes people who shouldn't be notified from the list."

    # Define a couple of helper functions...

    # ... one purely for logging purposes,
    def print_people_left(what_was_removed):
        "Print the number of people left after removing something."
        print("%d people for %d noms left after removing %s." %
              (len(people_to_notify),
               len(functools.reduce(operator.add,
                                    people_to_notify.values(),
                                    [])),
               what_was_removed))

    # ... and another simply to save keystrokes.
    def user_talk_pages():
        "A generator for valid user talk pages."
        titles = ["User talk:" + name for name in people_to_notify.keys()]
        for user_talk_page in [page for page in
                               pagegenerators.PagesFromTitlesGenerator(titles)
                               if page.exists() and not page.isRedirectPage()]:

            # First, a sanity check
            username = user_talk_page.title(withNamespace=False)
            if username not in people_to_notify:
                continue

            # Then yield the page and username
            yield (user_talk_page, username)

    # Prune empty entries
    people_to_notify = {k: v for k, v in people_to_notify.items() if k}
    print_people_left("empty entries")

    # Prune people I've already notified
    with open(CONFIG.get("dyknotifier", "ALREADY_NOTIFIED_FILE")) as already_notified_file:
        try:
            already_notified_data = json.load(already_notified_file)
        except ValueError as error:
            if error.message != "No JSON object could be decoded":
                raise
            else:
                already_notified_data = {}

        # Since the outer dict in the file is keyed on month string,
        # smush all the values together to get a dict keyed on username
        already_notified = {}
        for month_dict in already_notified_data.values():
            for month_username, month_items in month_dict.items():
                already_notified[month_username] =\
                    already_notified.get(month_username, []) + month_items

        # Now that we've built a dict, filter the list for each username
        for username, prior_nominations in already_notified.items():
            if username not in people_to_notify:
                continue

            prior_nominations = [CONFIG.get("dyknotifier", "NOMINATION_TEMPLATE") + x
                                 for x in prior_nominations]
            proposed = set(people_to_notify[username])
            people_to_notify[username] = list(proposed -
                                              set(prior_nominations))
        people_to_notify = {k: v for k, v in people_to_notify.items() if v}
        print_people_left("already-notified people")

    # Prune user talk pages that link to this nom.
    for user_talk_page, username in user_talk_pages():
        people_to_notify[username] = [nom for nom in people_to_notify[username]
                                      if nom not in user_talk_page.get()]
    people_to_notify = {k: v for k, v in people_to_notify.items() if v}
    print_people_left("linked people")

    return people_to_notify

# Disabling pylint because breaking stuff out into
# methods would spill too much into global scope

# pylint: disable=too-many-branches
# pylint: disable=too-many-statements
def notify_people(people_to_notify, args, wiki):
    "Adds a message to people who ought to be notified about their DYK noms."

    # Check if there's anybody to notify
    if len(people_to_notify) == 0:
        print("Nobody to notify.")
        return

    # Do the notification
    people_notified = dict()

    def write_notified_people_to_file():
        """Update the file of notified people with people_notified."""
        this_month = datetime.now().strftime("%B %Y")
        with open(CONFIG.get("dyknotifier", "ALREADY_NOTIFIED_FILE")) as already_notified_file:
            try:
                already_notified = json.load(already_notified_file)
            except ValueError as error:
                if error.message != "No JSON object could be decoded":
                    raise
                else:
                    already_notified = {} # eh, we'll be writing to it anyway

            already_notified_this_month = already_notified.get(this_month, {})
            with open(CONFIG.get("dyknotifier", "ALREADY_NOTIFIED_FILE"), "w") as already_notified_file:
                usernames = set(already_notified_this_month.keys() +
                                people_notified.keys())
                for username in usernames:
                    already_notified_this_month[username] = list(set(
                        already_notified_this_month.get(username, []) +\
                        people_notified.get(username, [])))

                already_notified[this_month] = already_notified_this_month

                # Remove all data from more than a year ago
                a_year_ago = datetime.today() - timedelta(365)
                def not_too_old(month):
                    """True if the given month was less than a year ago."""
                    return datetime.strptime(month, "%B %Y") > a_year_ago
                already_notified = {k: v for k, v in already_notified.items()
                                    if not_too_old(k)}

                json.dump(already_notified, already_notified_file)

        print("Wrote %d people for %d nominations this month." %
              (len(already_notified_this_month),
               len(functools.reduce(operator.add,
                                    already_notified_this_month.values(),
                                    []))))

    notify_iter = zip(people_to_notify.items(),
                      reversed(range(len(people_to_notify))))
    for (person, nom_names), counter in notify_iter:
        if args.count:
            edits_made = len(functools.reduce(operator.add,
                                              people_notified.values(), []))
            if edits_made >= args.count:
                print("%d notified; exiting.", edits_made)
                write_notified_people_to_file()
                sys.exit(0)

        # Remove namespaces from the nom names.
        nom_names = [name[34:] for name in nom_names]

        # Format nom names into a string
        nom_names_string = "".join(name.encode("utf-8") for name in nom_names)

        if args.interactive:
            print("About to notify {} for {}.".format(person.encode("utf-8"),
                                                      nom_names_string))
            choice = raw_input("What (s[kip], c[ontinue], q[uit])? ")
            if choice[0] == "s":
                if prompt.yn("Because I've already notified them?"):
                    people_notified[person] = people_notified.get(
                        person, []) + nom_names
                print("Skipping " + person + ".")
                continue
            elif choice[0] == "q":
                print("Stop requested; exiting.")
                write_notified_people_to_file()
                sys.exit(0)
        talkpage = pywikibot.Page(wiki, title="User talk:" + person)
        try:
            summary = CONFIG.get("dyknotifier", "SUMMARY").format(nom_names_string)
            talkpage.save(appendtext=generate_message(nom_names, wiki),
                          comment=summary)
            print("Success! Notified %s because of %s. (%d left)" %
                  (person.encode("utf-8"), nom_names_string, counter))
            people_notified[person] = people_notified.get(person,
                                                          []) + nom_names
        except pywikibot.Error as error:
            print("Couldn't notify {} because of {} - result: {}".format(person, nom_names_string, str(error)))
        except (KeyboardInterrupt, SystemExit):
            write_notified_people_to_file()
            raise
        except UnicodeEncodeError as e:
            traceback.print_exc()
            print("Unicode encoding error notifiying {} about {}: {}".format(person.encode("utf-8"), nom_names_string, str(e)))

    write_notified_people_to_file()

def get_who_to_nominate(wikitext, title):
    """
    Given the wikitext of a DYK nom and its title, return a dict of user
    talkpages of who to notify and the titles of the noms for which they
    should be notified).
    """
    if "#REDIRECT" in wikitext:
        print(title + " is a redirect.")
        return {}

    if "<small>" not in wikitext:
        print("<small> not found in " + title)
        return {}

    soup = BeautifulSoup(wikitext, "lxml")
    small_tags = [unicode(x.string) for x in soup.find_all("small")]
    def is_nom_string(text):
        "Is text the line in a DYK nom reading 'Created by... Nominated by...'?"
        return u"Nominated by" in text
    nom_lines = [tag for tag in small_tags if is_nom_string(tag)]
    if len(nom_lines) != 1:
        print(u"Small tags for " + title + u": " + unicode(small_tags))
        return {}

    # Every user whose talk page is linked to within the <small> tags
    # is assumed to have contributed. Looking for piped links to user
    # talk pages.
    usernames = usernames_from_text_with_sigs(nom_lines[0])

    # If there aren't any usernames, WTF and exit
    if len(usernames) == 0:
        print("WTF, no usernames for " + title.encode("utf-8"))
        return {}

    # The last one is the nominator.
    nominator = usernames[-1]

    # Removing all instances of nominator from usernames, since he or she
    # already knows about the nomination
    while nominator in usernames:
        usernames.remove(nominator)

    # Removing people who have contributed to the discussion
    discussion_text = wikitext[wikitext.find("</small>") + len("</small>"):]
    discussion = usernames_from_text_with_sigs(discussion_text)
    usernames = [user for user in usernames if user not in discussion]

    result = dict()
    for username in usernames:
        result[username] = title

    return result

def usernames_from_text_with_sigs(wikitext):
    "Returns the users whose talk pages are linked to in the wikitext."
    return [wikitext[m.end():m.end()+wikitext[m.end():].find("|")]\
            for m in re.finditer(r"User talk:", wikitext)]

def generate_message(nom_names, wiki):
    "Returns the template message to be placed on the nominator's talk page."

    # Check for nom subpage names that don't match up to articles in the hook
    def flag_subpage(nom_subpage_name):
        "Flag nom subpage names that don't correspond to articles."
        if "," not in nom_subpage_name:
            return (nom_subpage_name, False)

        the_subpage = pywikibot.Page(wiki, title=nom_subpage_name)

        return (nom_subpage_name, not the_subpage.exists())
    nom_names = [flag_subpage(x) for x in nom_names]
    message = u"\n\n{{{{subst:DYKNom|{0}|passive=yes}}}}"
    flagged_message = u"\n\n{{{{subst:DYKNom||passive=yes|section={0}}}}}"
    multiple_message = u"\n\n{{{{subst:DYKNom|{0}|passive=yes|multiple=yes}}}}"
    item = u"* {0} ([[Template:Did you know nominations/{1}|discussion]])"
    if len(nom_names) == 1:
        message_to_use = flagged_message if nom_names[0][1] else message
        return message_to_use.format(nom_names[0][0])
    else:
        wikitext_list = ""
        for nom_subpage_name, flagged in nom_names:
            main_item = ("Multiple articles"
                         if flagged
                         else ("[[" + nom_subpage_name + "]]"))
            wikitext_list += "\n" + item.format(main_item, nom_subpage_name)

        # Get rid of initial newline
        wikitext_list = wikitext_list[1:]

        return multiple_message.format(wikitext_list)

if __name__ == "__main__":
    main()
