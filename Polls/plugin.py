###
# Copyright (c) 2012, DAn
# All rights reserved.
#
#
###

import supybot.utils as utils
import supybot.ircdb as ircdb
from supybot.commands import *
import supybot.plugins as plugins
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks

import os
import traceback
import datetime
import supybot.ircmsgs as ircmsgs
import supybot.schedule as schedule

try:
    import sqlite3
except ImportError:
    from pysqlite2 import dbapi2 as sqlite3 # for python2.4


class Polls(callbacks.Plugin, plugins.ChannelDBHandler):
    """Poll for in channel
    Make polls and people can vote on them"""

    def __init__(self, irc):
        """run the usual init from parents"""
        callbacks.Plugin.__init__(self, irc)
        plugins.ChannelDBHandler.__init__(self)
        self.poll_schedules = [] # stores the current polls that are scheduled, so that on unload we can remove them
    
    def makeDb(self, filename):
        """ If db file exists, do connection and return it, else make new db and return connection to it"""

        if os.path.exists(filename):
            db = sqlite3.connect(filename, detect_types=sqlite3.PARSE_DECLTYPES|sqlite3.PARSE_COLNAMES)
            db.text_factory = str
            return db
        db = sqlite3.connect(filename, detect_types=sqlite3.PARSE_DECLTYPES|sqlite3.PARSE_COLNAMES)
        db.text_factory = str
        cursor = db.cursor()

        self.executeQuery(cursor, """CREATE TABLE polls(
                    id INTEGER PRIMARY KEY,
                    started_time TIMESTAMP,         -- time when poll was created
                    isAnnouncing INTEGER default 1, -- if poll is announcing to channel
                    closed TIMESTAMP,               -- NULL by default, set to time when closed(no more voting allowed)
                    question TEXT)""", None)
        self.executeQuery(cursor, """CREATE TABLE choices(
                    poll_id INTEGER,
                    choice_num INTEGER,
                    choice TEXT)""", None)
        self.executeQuery(cursor, """CREATE TABLE votes(
                    id INTEGER PRIMARY KEY,
                    poll_id INTEGER,
                    voter_nick TEXT,
                    voter_host TEXT,
                    choice INTEGER,
                    time timestamp)""", None)
        db.commit()
        return db

    def executeQuery(self, cursor, queryString, sqlargs):
        """ Executes a SqLite query
            in the given Db """
      
        try:
            if sqlargs is None:
                cursor.execute(queryString)
            else:
                cursor.execute(queryString,sqlargs)
        except Exception, e:
            cursor = None
            self.log.error('Error with sqlite execute: %s' % e)
            self.log.error('For QueryString: %s' % queryString)

        return cursor    

    def _runPoll(self, irc, channel, pollid):
        """Run by supybot schedule, outputs poll question and choices into channel at set interval"""

        db = self.getDb(channel)
        cursor = db.cursor()
        self.executeQuery(cursor, 'SELECT isAnnouncing,closed,question FROM polls WHERE id=?', (pollid,))
        is_announcing, closed, question = cursor.fetchone()

        # if poll shouldnt be announcing or is closed, then stop schedule
        if (not is_announcing) or closed:
            try:
                schedule.removeEvent('%s_poll_%s' % (channel, pollid))
                self.poll_schedules.remove('%s_poll_%s' % (channel, pollid))
            except:
                self.log.warning('_runPoll Failed to remove schedule event')
            return

        irc.sendMsg(ircmsgs.privmsg(channel, 'Poll #%s: %s' % (pollid, question)))

        self.executeQuery(cursor, 'SELECT choice_num,choice FROM choices WHERE poll_id=? ORDER BY choice_num', (pollid,))

        # output all of the polls choices
        choice_row = cursor.fetchone()
        while choice_row is not None:
            irc.sendMsg(ircmsgs.privmsg(channel, ('%s: %s' % (choice_row[0], choice_row[1]))))
            choice_row = cursor.fetchone()
        
        irc.sendMsg(ircmsgs.privmsg(channel, 'To vote, do !vote %s <choice number>' % pollid)) 


    def newpoll(self, irc, msg, args, channel, interval, answers, question):
        """<number of minutes for announce interval> <"answer, answer, ..."> question
        op command to add a new poll"""

        capability = ircdb.makeChannelCapability(channel, 'op')
        if not ircdb.checkCapability(msg.prefix, capability):
            irc.error('Need ops')
            return

        db = self.getDb(channel)
        cursor = db.cursor()
        self.executeQuery(cursor, 'INSERT INTO polls VALUES (?,?,?,?,?)', (None, datetime.datetime.now(), 1, None, question))
        pollid = cursor.lastrowid

        # used to add choices into db
        def genAnswers():
            for i, answer in enumerate(answers, start=1):
                yield pollid, i, answer

        cursor.executemany('INSERT INTO choices VALUES (?,?,?)', genAnswers())

        db.commit()

        irc.reply('Started new poll #%s' % pollid)

        # function called by schedule event. can not have args
        def runPoll():
            self._runPoll(irc, channel, pollid)

        # start schedule. will announce poll/choices to channel at interval
        schedule.addPeriodicEvent(runPoll, interval*60, name='%s_poll_%s' % (channel, pollid))
        self.poll_schedules.append('%s_poll_%s' % (channel, pollid))

        irc.replySuccess()

    newpoll = wrap(newpoll, ['channeldb', 'Op', 'positiveInt', commalist('something'), 'text'])

    def vote(self, irc, msg, args, channel, pollid, choice):
        """<poll id number> <choice number>
        public command to vote on poll"""

        db = self.getDb(channel)
        cursor = db.cursor()

        # query to check that poll exists and it isnt closed
        self.executeQuery(cursor, 'SELECT closed FROM polls WHERE id=?', (pollid,))
        result = cursor.fetchone()
        if result is None:
            irc.error('No poll with that id')
            return
        if result[0] is not None:
            irc.error('This poll was closed on %s' % result[0].strftime('%Y-%m-%d at %-I:%M %p'))
            return

        # query to check that their choice exists
        self.executeQuery(cursor, 'SELECT * FROM choices WHERE poll_id=? AND choice_num=?', (pollid, choice))
        result = cursor.fetchone()
        if result is None:
            irc.error('That is not a choice for that poll')
            return
        
        # query to check they havnt already voted on this poll
        self.executeQuery(cursor, 'SELECT choice,time FROM votes WHERE (voter_nick=? OR voter_host=?) AND poll_id=?', (msg.nick, msg.host, pollid))
        result = cursor.fetchone()
        if result is not None:
            irc.error('You have already voted for %s on %s' % (result[0], result[1].strftime('%Y-%m-%d at %-I:%M %p')))
            return

        # query to insert their vote
        self.executeQuery(cursor, 'INSERT INTO votes VALUES (?,?,?,?,?,?)', (None, pollid, msg.nick, msg.host, choice, datetime.datetime.now()))
        db.commit()

        irc.sendMsg(ircmsgs.privmsg(channel, 'Your vote on poll #%s for %s has been inputed, sending you results in PM' % (pollid, choice)))

        irc.sendMsg(ircmsgs.privmsg(msg.nick, 'Here is results for poll #%s, you just voted for %s' % (pollid, choice)))

        # query loop thru each choice for this poll, and for each choice another query to grab number of votes, and output
        cursor2 = db.cursor()
        self.executeQuery(cursor, 'SELECT choice_num,choice FROM choices WHERE poll_id=? ORDER BY choice_num', (pollid,))
        choice_row = cursor.fetchone()
        while choice_row is not None:
            self.executeQuery(cursor2, 'SELECT count(*) FROM votes WHERE poll_id=? AND choice=?', (pollid, choice_row[0],))
            vote_row = cursor2.fetchone()
            irc.sendMsg(ircmsgs.privmsg(msg.nick, '%s: %s - %s votes' % (choice_row[0], choice_row[1], vote_row[0])))
            choice_row = cursor.fetchone()

    vote = wrap(vote, ['channeldb', 'positiveInt', 'positiveInt'])

    def results(self, irc, msg, args, channel, pollid):
        """[channel] <pollid>
        public command to PM you results of poll. You have to had voted already"""

        db = self.getDb(channel)
        cursor = db.cursor()

        # query to make sure this poll exists. make new cursor since we will use it further below to output results
        cursor1 = db.cursor()
        self.executeQuery(cursor, 'SELECT choice_num,choice FROM choices WHERE poll_id=? ORDER BY choice_num', (pollid,))
        choice_row = cursor1.fetchone()
        if choice_row is None:
            irc.error('I dont think that poll id exists')
            return

        # query to make sure they have already voted on this poll
        self.executeQuery(cursor, 'SELECT id FROM votes WHERE poll_id=? AND (voter_nick=? OR voter_host=?)', (pollid, msg.nick, msg.host))
        result = cursor.fetchone()
        if result is None:
            irc.error('You need to vote first to view results!')
            return

        irc.sendMsg(ircmsgs.privmsg(msg.nick, 'Here is results for poll #%s' % pollid))

        # query loop thru each choice for this poll, and for each choice another query to grab number of votes, and output
        cursor2 = db.cursor()
        while choice_row is not None: 
            self.executeQuery(cursor2, 'SELECT count(*) FROM votes WHERE poll_id=? AND choice=?', (pollid, choice_row[0],))
            vote_row = cursor2.fetchone()
            irc.sendMsg(ircmsgs.privmsg(msg.nick, '%s: %s - %s votes' % (choice_row[0], choice_row[1], vote_row[0])))
            choice_row = cursor1.fetchone()

        irc.replySuccess()

    results = wrap(results, ['channeldb', 'positiveInt'])

    #TODO finish this command...
    def openpolls(self, irc, msg, args, channel):
        """takes no arguments
        public command to PM you list of all polls"""
        db = self.getDb(channel)
        cursor = db.cursor()
        self.executeQuery(cursor, 'SELECT id,question FROM polls WHERE closed is NULL', None)
        row = cursor.fetchone()
        
        while row is not None:
            irc.sendMsg(ircmsgs.privmsg(msg.nick, '%s: %s' % (row[0], row[1])))
            cursorChoice = db.cursor()
            self.executeQuery(cursorChoice, 'SELECT choice_num,choice FROM choices WHERE poll_id=? ORDER BY choice_num', (row[0],))
            choiceRow = cursorChoice.fetchone()
            irc.sendMsg(ircmsgs.privmsg(msg.nick, 'The choices are as follows :- '))
            while choiceRow is not None:
                irc.sendMsg(ircmsgs.privmsg(msg.nick, '%s: %s' % (choiceRow[0], choiceRow[1])))
                choiceRow = cursorChoice.fetchone()
            row = cursor.fetchone()

    openpolls = wrap(openpolls, ['channeldb'])

    def pollon(self, irc, msg, args, channel, pollid, interval):
        """[channel] <pollid> <interval in minutes>
        op command to turn a poll schedule on so it is announcing, with interval"""

        db = self.getDb(channel)
        cursor = db.cursor()

        # query to check poll exists, and if it is already on
        self.executeQuery(cursor, 'SELECT isAnnouncing,closed FROM polls WHERE id=?', (pollid,))
        result = cursor.fetchone()
        if result is None:
            irc.error('That poll id does not exist')
            return
        if result[0] == 1:
            irc.error('Poll is already active')
            return
        
        # query to set poll off
        db.execute('UPDATE polls SET isAnnouncing=? WHERE id=?', (1, pollid))
        db.commit()

        if result[1] is not None:
            irc.reply('Note: you are turning on closed poll. I will not start announcing it')
            return

        # function called by schedule event. can not have args
        def runPoll():
            self._runPoll(irc, channel, pollid)

        # start schedule. will announce poll/choices to channel at interval
        schedule.addPeriodicEvent(runPoll, interval*60, name='%s_poll_%s' % (channel, pollid))
        self.poll_schedules.append('%s_poll_%s' % (channel, pollid))

        irc.replySuccess()

    pollon = wrap(pollon, ['channeldb', 'Op', 'positiveInt', 'positiveInt'])

    def polloff(self, irc, msg, args, channel, pollid):
        """[channel] <pollid>
        op command to stop a poll schedule from announcing"""

        db = self.getDb(channel)
        cursor = db.cursor()

        # query to grab poll info, then check it exists, isnt already off, and warn them if it is closed
        self.executeQuery(cursor, 'SELECT isAnnouncing,closed FROM polls WHERE id=?', (pollid,))
        result = cursor.fetchone()
        if result is None:
            irc.error('That poll id does not exist')
            return
        if result[0] == 0:
            irc.error('Poll is already off')
            return
        if result[1] is not None:
            irc.reply('Note: you are turning off a closed poll')

        # iquery to turn the poll "off", meaning it wont be scheduled to announce
        self.executeQuery(cursor, 'UPDATE polls SET isAnnouncing=? WHERE id=?', (0, pollid))
        db.commit()

        try:
            schedule.removeEvent('%s_poll_%s' % (channel, pollid))
            self.poll_schedules.remove('%s_poll_%s' % (channel, pollid))
        except:
            irc.error('Removing scedule failed')

        irc.replySuccess()

    polloff = wrap(polloff, ['channeldb', 'Op', 'positiveInt'])

    def closepoll(self, irc, msg, args, channel, pollid):
        """[channel] <pollid>
        op command to close poll. No more voting allowed."""

        db = self.getDb(channel)
        cursor = db.cursor()

        # query to check poll exists and if it is closed
        self.executeQuery(cursor, 'SELECT isAnnouncing,closed FROM polls WHERE id=?', (pollid,))
        result = cursor.fetchone()
        if result is None:
            irc.error('Poll id doesnt exist')
            return
        if result[1] is not None:
            irc.error('Poll already closed on %s' % result[1].strftime('%Y-%m-%d at %-I:%M %p'))
            return

        # close the poll in db
        self.executeQuery(cursor, 'UPDATE polls SET closed=? WHERE id=?', (datetime.datetime.now(), pollid))
        db.commit()

        try:
            schedule.removeEvent('%s_poll_%s' % (channel, pollid))
            self.poll_schedules.remove('%s_poll_%s' % (channel, pollid))
        except:
            self.log.warning('Failed to remove schedule event')

        irc.replySuccess()

    closepoll = wrap(closepoll, ['channeldb', 'Op', 'positiveInt'])

    def openpoll(self, irc, msg, args, channel, pollid, interval):
        """[channel] <pollid>
        op command to open poll for voting. Starts schedule to do announcing if set to active"""

        db = self.getDb(channel)
        cursor = db.cursor()

        # query to check poll exists and if it is open
        self.executeQuery(cursor, 'SELECT isAnnouncing,closed FROM polls WHERE id=?', (pollid,))
        result = cursor.fetchone()
        if result is None:
            irc.error('Poll id doesnt exist')
            return
        if result[1] is None:
            irc.error('Poll is still open')
            return

        # query to OPEN IT UP! unsets closed time
        self.executeQuery(cursor, 'UPDATE polls SET closed=? WHERE id=?', (None, pollid))
        db.commit()

        # if poll was set active then start schedule for it
        if result[0] == 1:
            if interval is None:
                irc.reply('Note: Poll set to active, but you didnt supply interval, using default of 10 minutes')
                interval = 10
            # function called by schedule event. can not have args
            def runPoll():
                self._runPoll(irc, channel, pollid)

            # start schedule. will announce poll/choices to channel at interval
            schedule.addPeriodicEvent(runPoll, interval*60, name='%s_poll_%s' % (channel, pollid))
            self.poll_schedules.append('%s_poll_%s' % (channel, pollid))

    openpoll = wrap(openpoll, ['channeldb', 'Op', 'positiveInt', additional('positiveInt')])

    def die(self):
        for schedule_name in self.poll_schedules:
            schedule.removeEvent(schedule_name)

Class = Polls


# vim:set shiftwidth=4 softtabstop=4 expandtab:
