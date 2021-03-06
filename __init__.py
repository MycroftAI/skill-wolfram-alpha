# Copyright 2016 Mycroft AI, Inc.
#
# This file is part of Mycroft Core.
#
# Mycroft Core is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Mycroft Core is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Mycroft Core.  If not, see <http://www.gnu.org/licenses/>.


from StringIO import StringIO

import re
import wolframalpha
from os.path import dirname, join
from requests import HTTPError

from mycroft.api import Api
from mycroft.messagebus.message import Message
from mycroft.skills.core import MycroftSkill
from mycroft.util.log import getLogger
from mycroft.util.parse import normalize

__author__ = 'seanfitz'

LOG = getLogger(__name__)


class EnglishQuestionParser(object):
    """
    Poor-man's english question parser. Not even close to conclusive, but
    appears to construct some decent w|a queries and responses.
    """

    def __init__(self):
        self.regexes = [
            re.compile(
                ".*(?P<QuestionWord>who|what|when|where|why|which|whose) "
                "(?P<Query1>.*) (?P<QuestionVerb>is|are|was|were) "
                "(?P<Query2>.*)"),
            re.compile(
                ".*(?P<QuestionWord>who|what|when|where|why|which|how) "
                "(?P<QuestionVerb>\w+) (?P<Query>.*)")
        ]

    def _normalize(self, groupdict):
        if 'Query' in groupdict:
            return groupdict
        elif 'Query1' and 'Query2' in groupdict:
            return {
                'QuestionWord': groupdict.get('QuestionWord'),
                'QuestionVerb': groupdict.get('QuestionVerb'),
                'Query': ' '.join([groupdict.get('Query1'), groupdict.get(
                    'Query2')])
            }

    def parse(self, utterance):
        for regex in self.regexes:
            match = regex.match(utterance)
            if match:
                return self._normalize(match.groupdict())
        return None


class WAApi(Api):
    def __init__(self):
        super(WAApi, self).__init__("wa")

    def get_data(self, response):
        return response

    def query(self, input):
        data = self.request({"query": {"input": input}})
        return wolframalpha.Result(StringIO(data.content))


class WolframAlphaSkill(MycroftSkill):
    PIDS = ['Value', 'NotableFacts:PeopleData', 'BasicInformation:PeopleData',
            'Definition', 'DecimalApproximation']

    def __init__(self):
        MycroftSkill.__init__(self, name="WolframAlphaSkill")
        self.__init_client()
        self.question_parser = EnglishQuestionParser()

    def __init_client(self):
        key = self.config.get('api_key')
        if key and not self.config.get('proxy'):
            self.client = wolframalpha.Client(key)
        else:
            self.client = WAApi()

    def initialize(self):
        self.init_dialog(dirname(__file__))
        self.emitter.on('intent_failure', self.handle_fallback)

    def get_result(self, res):
        try:
            return next(res.results).text
        except:
            result = None
            try:
                for pid in self.PIDS:
                    result = self.__find_pod_id(res.pods, pid)
                    if result:
                        result = result[:5]
                        break
                if not result:
                    result = self.__find_num(res.pods, '200')
                return result
            except:
                return result

    # TODO: Localization
    def handle_fallback(self, message):
        utt = message.data.get('utterance')
        LOG.debug("WolframAlpha fallback attempt: " + utt)
        lang = message.data.get('lang')
        if not lang:
            lang = "en-us"

        utterance = normalize(utt, lang)
        parsed_question = self.question_parser.parse(utterance)

        query = utterance
        others = []
        if parsed_question:
            # Try to store pieces of utterance (None if not parsed_question)
            utt_word = parsed_question.get('QuestionWord')
            utt_verb = parsed_question.get('QuestionVerb')
            utt_query = parsed_question.get('Query')
            if utt_verb == "'s":
                utt_verb = 'is'
                parsed_question['QuestionVerb'] = 'is'
            query = "%s %s %s" % (utt_word, utt_verb, utt_query)
            phrase = "know %s %s %s" % (utt_word, utt_query, utt_verb)
            LOG.debug("Falling back to WolframAlpha: " + query)
        else:
            # This utterance doesn't look like a question, don't waste
            # time with WolframAlpha.

            # TODO: Log missed intent
            LOG.debug("Unknown intent: " + utterance)
            return

        try:
            self.enclosure.mouth_think()
            res = self.client.query(query)
            result = self.get_result(res)
            if result is None:
                others = self._find_did_you_mean(res)
        except HTTPError as e:
            if e.response.status_code == 401:
                self.emitter.emit(Message("mycroft.not.paired"))
            return
        except Exception as e:
            LOG.exception(e)
            self.speak_dialog("not.understood", data={'phrase': phrase})
            return

        if result:
            input_interpretation = self.__find_pod_id(res.pods, 'Input')
            verb = "is"
            structured_syntax_regex = re.compile(".*(\||\[|\\\\|\]).*")
            if parsed_question:
                if not input_interpretation or structured_syntax_regex.match(
                        input_interpretation):
                    input_interpretation = parsed_question.get('Query')
                verb = parsed_question.get('QuestionVerb')

            if "|" in result:  # Assuming "|" indicates a list of items
                verb = ":"

            result = self.process_wolfram_string(result)
            input_interpretation = \
                self.process_wolfram_string(input_interpretation)
            response = "%s %s %s" % (input_interpretation, verb, result)

            self.speak(response)
        else:
            if len(others) > 0:
                self.speak_dialog('others.found',
                                  data={'utterance': utterance,
                                        'alternative': others[0]})
            else:
                self.speak_dialog("not.understood", data={'phrase': phrase})

    @staticmethod
    def __find_pod_id(pods, pod_id):
        for pod in pods:
            if pod_id in pod.id:
                return pod.text
        return None

    @staticmethod
    def __find_num(pods, pod_num):
        for pod in pods:
            if pod.node.attrib['position'] == pod_num:
                return pod.text
        return None

    @staticmethod
    def _find_did_you_mean(res):
        value = []
        root = res.tree.find('didyoumeans')
        if root is not None:
            for result in root:
                value.append(result.text)
        return value

    def process_wolfram_string(self, text):
        # Remove extra whitespace
        text = re.sub(r" \s+", r" ", text)

        # Convert | symbols to commas
        text = re.sub(r" \| ", r", ", text)

        # Convert newlines to commas
        text = re.sub(r"\n", r", ", text)

        # Convert !s to factorial
        text = re.sub(r"!", r",factorial", text)

        with open(join(dirname(__file__), 'regex',
                       self.lang, 'list.rx'), 'r') as regex:
            list_regex = re.compile(regex.readline())

        match = list_regex.match(text)
        if match:
            text = match.group('Definition')

        return text

    def shutdown(self):
        self.emitter.remove('intent_failure', self.handle_fallback)
        super(WolframAlphaSkill, self).shutdown()

    def stop(self):
        pass


def create_skill():
    return WolframAlphaSkill()
