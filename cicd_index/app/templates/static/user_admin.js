{% include "static/tools.js" %}

function return_to_branches() {
    location = '/index';
}

function delete_user() {
    var form = webix.ui({
        view: "window", 
        position: 'center',
        modal: true,
        head: "Delete Instance",
        width: 550,
        body: {
            view: 'form',
            complexData: true,
            elements: [
                // { view:"combo", name: 'dump', label:"Dump", options: dumps },
                { view: 'template', template: "Delete user"},
                {
                    cols:[
                        { view:"button", value:"OK", css:"webix_primary", click: function() { 
                                var values = this.getParentView().getFormView().getValues();
                                form.hide();
                                webix.message("Deleting in Background", "info");
                                webix.ajax().post('/cicd/data/user/delete', {
                                    'id': current_details,
                                }).then(function(data) {
                                    webix.message("User erased: " + current_details, "info");
                                    reload_table($$("table-users"));
                                    form.hide();
                                }).fail(function(data) {
                                    alert(data.statusText);
                                    console.error(data.responseText);
                                });
                                }
                        },
                        { view:"button", value:"Cancel", click: function() {
                            form.hide();
                        }}
                    ]
                }
            ],
            on: {
                'onSubmit': function() {
                },
            }
        }
    });
    form.show();
}

function new_user() {
    edit_user('new');
}

function edit_user(id) {
    webix.ajax().get('/cicd/data/user', {'id': id}).then(function(data) {
        data = data.json();
        var form = webix.ui({
            view: "window", 
            position: 'center',
            modal: true,
            head: "Settings",
            width: 550,
            body: {
                view: 'form',
                complexData: true,
                elements: [
                    { view: 'text', name: 'login', label: "Login" },
                    { view: 'text', name: 'name', label: "Name" },
                    { view: "checkbox", name: 'is_admin', label:"Is Admin" },
                    { view: "text", name: 'password', label:"Password" },
                    {
                        cols:[
                            { view:"button", value:"OK", css:"webix_primary", click: function() { 
                                var values = this.getParentView().getFormView().getValues();
                                webix.ajax().post('/cicd/data/user', values).then(function() {
                                    form.hide();
                                    if (id == 'new') {
                                        reload_table($$("table-users"));
                                    }
                                    else {
                                        reload_table_item(
                                            $$("table-users"),
                                            $$("table-users").getSelectedItem()._id,
                                            values,
                                        )
                                    }
                                });
                                }
                            },
                            { view:"button", value:"Cancel", click: function() {
                                form.hide();
                            }}
                        ]
                    }
                ],
                on: {
                    'onSubmit': function() {
                    },
                }
            }
        });
        form.getChildViews()[1].setValues(data[0]);
        form.show();
    });
    return false;
}

var current_details = null;
function reload_user_details(id) {
    webix.ajax().get('/cicd/data/users?id=' + id).then(function(data) {
        var template = $$('webix-user-details');
        template.data = data.json()[0];
        template.refresh();
        template.show();
        current_details = id;

        // load user sites;
        var table = $$("table-user-sites");
        if (current_details) {
            table.clearAll();
            table.load("/cicd/data/user_sites?user_id=" + current_details);
            table.show();
        }
        else {
            table.hide();
        }
    }).fail(function(response) {
        webix.message("Error: " + response.statusText, "error");
    });

}

webix.ui({
    type: 'wide',
    cols: [
        {
            rows: [
                {
                    view: "template",
                    type: "header",
                    css: "webix_dark",
                    template: "Users"
                },
                {
                    view: 'toolbar',
                    css: "webix_dark",
                    id: 'site-toolbar-common',
                    elements: [
                        { view:"button", id:"return_to_branches", value:"Return", click: clicked_menu},
                        { view:"button", id:"new_user", value:"New User", click: clicked_menu},
                    ],
                },
                {
                    view:"text", 
                    placeholder:"Filter grid",
                    on:{
                        onTimedKeyPress:function(){
                            var text = this.getValue().toLowerCase();
                            var table = $$("table-users");
                            var columns = table.config.columns;
                            table.filter(function(obj){
                                for (var i=0; i<columns.length; i++) {
                                    if (obj[columns[i].id].toString().toLowerCase().indexOf(text) !== -1) return true;
                                return false;
                                }
                            })
                        }
                    }
                },
                {
                    id: 'table-users',
                    view: "datatable",
                    navigation: true,
                    headerRowHeight: 60,
                    rowHeight: 30,
                    select: 'row',
                    autoConfig: false,
                    url: '/cicd/data/users',
                    editable: false,
                    data: [],
                    leftSplit: 0,
                    scrollX: false,
                    on: {
                        onSelectChange:function(){
                            if (!this.getSelectedItem()) {
                                return;
                            }
                            reload_user_details(this.getSelectedItem()._id);
                        },
                        onItemClick: function(id, e, trg) {
                            //if (id.column === 'start_instance') {
                            //    var name = this.getSelectedItem().name;
                            //    start_instance(name);
                            //}
                        }
                    },
                    columns:[
                        { id: 'login', header: 'Login', minWidth: 250},
                        { id: 'name', header: 'Name', minWidth: 350},
                    ],
                },
            ]
        },
        {
        rows: [
            {
                view: 'toolbar',
                css: "webix_dark",
                id: 'site-toolbar',
                elements: [
                    { view:"button", id:"edit_user", value:"Edit", width:150, align:"left", click: clicked_menu },
                    { view:"button", id:"delete_user", value:"Delete", width:150, align:"right", click: clicked_menu },
                    /*
                    { view:"button", id:"start", value:"Open UI", width:100, align:"right", click: start_instance },
                    { view:"button", id:"start_mails", value:"Mails", width:100, align:"right", click: show_mails },
                    { view:"button", id:"start_logging", value:"Live Log", width:100, align:"right", click: show_logs },
                    { view:"button", id:"start_shell", value:"Shell", width:100, align:"right", click: shell },
                    { view:"button", id:"start_debugging", value:"Debug", width:100, align:"right", click: debug },
                    */
                ],
            },
            {
                id: "webix-user-details",
                maxWidth: 650,
                css: "webix_dark",
                view: "template",
                type: "body",
                template: "html->user-template",
                hidden: true,
            },
            {
                id: 'table-user-sites',
                view: "datatable",
                hidden: true,
                navigation: true,
                headerRowHeight: 60,
                rowHeight: 30,
                select: 'row',
                autoConfig: false,
                url: '/cicd/data/user_sites',
                editable: true,
                data: [],
                leftSplit: 0,
                scrollX: false,
                on: {
                    onCheck: function(row, column, state) {
                        var item = $$("table-user-sites").getItem(row);
                        webix.ajax().post('/cicd/data/user_sites', {
                            'user_id': current_details,
                            'name': item.name,
                            'allowed': state,
                            }).then(function(res) {
                        });
                    }
                },
                columns:[
                    { id: 'name', header: 'Name', minWidth: 250},
                    { id: 'allowed', header: 'Allowed', template: "{common.checkbox()}", disable: true},
                ],
            },
        ],
        }
    ]
});

webix.ui.fullScreen();