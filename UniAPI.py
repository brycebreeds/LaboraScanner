import json
from datetime import date

import requests
# from asgiref.timeout import timeout
def custom_serializer(obj):
    if isinstance(obj, date):
        return obj.isoformat()  # Convert date to ISO string
    raise TypeError(f"Type {type(obj)} not serializable")


class UniAPI(object):
    def __init__(self, baseurl, token="", entityID=None, debug=False):
        self.session = requests.session()
        self.entityID = entityID
        self.token = token
        self.baseurl = baseurl
        self.debug = debug

        self.flogin(self.token)

    def do_post(self, url, data, timeout=10):
        if self.debug:
            data["_SQLDEBUG"] = ""

        try:
            r = self.session.post(url, data, timeout=timeout)
        except Exception as e:
            print(e)
            return {"http_code": 666}
        return r.text

    def do_get(self, url, data, timeout=10):
        if self.debug:
            data["_SQLDEBUG"] = ""
        try:
            r = self.session.get(url, params=data, timeout=timeout)
        except Exception as e:
            print(e)
            return {"http_code": 666}
        return r.text

    def get_entity_property(self, entity_id, property_names):
        data = {
            "entityID": entity_id,
            "properties": json.dumps(property_names),
            "token": self.token
        }
        r = self.do_get(self.baseurl + "uniappapi/get_entity_property", data)
        # print(r)
        resData = json.loads(r)
        if resData["http_code"] == 200:
            return resData["detail"]

        print(resData)
        return False

    def update_entity_property(self, entity_id, property_data):
        #property data ex
        #{"key": "value"}
        data = property_data

        data["entityID"] = entity_id
        data["token"] = self.token

        r = self.do_post(self.baseurl + "uniappapi/update_entity_property", data)
        resData = json.loads(r)
        success = True
        failedProps = []
        if resData["http_code"] == 200:
            for prop in property_data:
                if prop == "entityID":
                    continue
                if prop == "token":
                    continue
                if property_data[prop] != resData["detail"][prop]:
                    success = False
                    failedProps.append(prop)
            resData["detail"]["failedProps"] = failedProps
            return resData["detail"]

        return False

    def update_related_entity_property(self, entity_id, property_data, set_name, dir="child"):
        data = property_data
        data["entityID"] = entity_id
        data["setName"] = set_name
        data["dir"] = dir
        data["token"] = self.token

        r = self.do_post(self.baseurl + "uniappapi/update_related_entity_data", data)
        resData = json.loads(r)
        success = True
        failedProps = []
        if resData["http_code"] == 200:
            for prop in property_data:
                if prop == "entityID":
                    continue
                if prop == "token":
                    continue
                if prop == "setName":
                    continue
                if prop == "dir":
                    continue
                if property_data[prop] != resData["detail"][prop]:
                    success = False
                    failedProps.append(prop)
            resData["detail"]["failedProps"] = failedProps
            return resData["detail"]

        return False

    def update_entity_property_if_last_updated_by_user(self, entity_id, property_data):
        # property data ex
        # {"key": "value"}
        data = property_data

        data["entityID"] = entity_id
        data["token"] = self.token

        r = self.do_post(self.baseurl + "uniappapi/update_entity_property_if_last_updated_by_user", data)
        resData = json.loads(r)
        unchangedProps = []
        errorProps = []
        if resData["http_code"] == 200:
            for prop in property_data:
                if prop == "entityID":
                    continue
                if prop == "token":
                    continue
                if prop == "_SQLDEBUG":
                    continue
                if prop == 'error':
                    continue

                if not resData["detail"][prop]:
                    unchangedProps.append(prop)

                if isinstance(resData["detail"][prop], dict):
                    equal_value = resData['detail'][prop]["propValueEqual"]
                    equal_user = resData['detail'][prop]["userEqual"]
                    if not equal_value and not equal_user:
                        errorProps.append(prop)
            resData["detail"]["errorProps"] = errorProps
            return resData["detail"]

        return False

    def bulk_individual_entity_property_update(self, array):
        data = {
            "updateArray": json.dumps(array, default=custom_serializer),
            "token": self.token,
            "updateIfLastUpdateByUser": "true"
        }
        r = self.do_post(self.baseurl + "uniappapi/bulk_individual_entity_property_update", data)
        resData = json.loads(r)
        failedProps = []
        if resData["http_code"] == 200:
            resData["detail"]["failedProps"] = failedProps
            return resData["detail"]

        return False

    def get_related_entity_data(self, entity_id, set_name, setgroup_name, property_names, direction="child" ):
        data = {
            "entityID": entity_id,
            "setName": set_name,
            "setGroup": setgroup_name,
            "properties": json.dumps(property_names),
            "dir": direction,
            "token": self.token,
            "_SQLDEBUG": ""
        }
        r = self.do_get(self.baseurl + "uniappapi/get_related_entity_data", data, 200)
        resData = json.loads(r)
        if resData["http_code"] == 200:
            return resData["detail"]

        print(resData)
        return False

    def flogin(self, floginToken):
        data = {
            'token': floginToken.replace("?fl=", "")
        }
        r = self.do_post(self.baseurl + 'uniappapi/token_login', data)
        try:
            resData = json.loads(r)
            if resData['http_code'] == 200:
                self.token = resData['detail']['token']
                self.entityID = resData['detail']['id']
                return resData["detail"]
        except Exception as e:
            print(e)
            print(r)
        return False

    def login(self, username, password):
        data = {
            'username': username,
            'password': password,
        }
        r = self.do_post(self.baseurl + 'uniappapi/login', data)
        resData = json.loads(r)
        if resData['http_code'] == 200:
            self.token = resData['detail']['token']
            self.entityID = resData['detail']['id']
            return True

        print(resData)
        return False

    def get_all_values_for_property(self, property_name):
        data = {
            "property": property_name,
            "token": self.token,
            "_SQLDEBUG": ""
        }
        r = self.do_get(self.baseurl + "uniappapi/get_all_values_for_property", data)
        resData = json.loads(r)
        if resData["http_code"] == 200:
            return resData["detail"]

        print(resData)
        return False

    def get_entity_id_from_unique_property_value(self, setgroup_id, property_name, search_term):
        data = {
            "setgroupID": setgroup_id,
            "property": property_name,
            "searchTerm": search_term,
            "token": self.token
        }
        r = self.do_get(self.baseurl + "uniappapi/get_entity_id_from_unique_property_value", data)
        try:
            resData = json.loads(r)
            if resData["http_code"] == 200:
                return resData["detail"]
        except Exception as e:
            print(r)
        return False

    def create_new_entity(self, parent_entity_id, related_entity_pageflow_id, properties):
        # property data ex
        # {"key": "value"}
        data = properties

        data["token"] = self.token
        data["relatedEntityPageflowID"] = related_entity_pageflow_id
        data["parentEntityID"] = parent_entity_id

        r = self.do_post(self.baseurl + "uniappapi/create_new_entity", data)
        resData = json.loads(r)
        if resData["http_code"] == 200:
            return resData["detail"]

        return False

if __name__ == '__main__':
    # test = UniAPI("https://manage.unisite.com/intranet/")
    # test.login('Oridbn', 'Odbn@123')
    # test.get_entity_property(test.entityID, json.dumps(["ForceLoginUrl", "LandingLayoutUrl", "PinLayout"]))
    test2 = UniAPI("https://scannertest.oriquote.com/site/", token="urqI59aZpuawpKCg0obK0KzPimeSl7bgnamb4bmZ2ajGo4Oo", entityID="146497", debug=True)
    # res = test2.get_all_values_for_property('FunderMemberId')
    # print(test2.token, test2.entityID)