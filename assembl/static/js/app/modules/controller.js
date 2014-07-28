define(function(require){
    'use strict';

    var Marionette = require('marionette'),
           Assembl = require('modules/assembl'),
               Ctx = require('modules/context'),
      groupManager = require('modules/groupManager'),
                 $ = require('jquery');

    var SegmentList = require('views/segmentList'),
           IdeaList = require('views/ideaList'),
          IdeaPanel = require('views/ideaPanel'),
        MessageList = require('views/messageList'),
          Synthesis = require('models/synthesis'),
     SynthesisPanel = require('views/synthesisPanel'),
               User = require('models/user'),
             navBar = require('views/navBar'),
       Notification = require('views/notification'),
  CollectionManager = require('modules/collectionManager');

    var Controller = Marionette.Controller.extend({

        initialize: function(){
           window.assembl = {};

           var collectionManager = new CollectionManager();
            /**
             * fulfill app.currentUser
             */
            function loadCurrentUser(){
              var user;
                if( Ctx.getCurrentUserId() ){
                  user = new User.Model();
                  user.fetchFromScriptTag('current-user-json');
                }
                else {
                  user = new User.Collection().getUnknownUser();
                }
                user.fetchPermissionsFromScripTag();
                Ctx.setCurrentUser(user);
                Ctx.loadCsrfToken(true);
            }
            loadCurrentUser();
            //We only need this here because we still use deprecated access functions
            collectionManager.getAllUsersCollectionPromise();


            /*$w.segmentList = new SegmentList({el: '#segmentList', button: '#button-segmentList'}).render();

            // Idea list
            $w.ideaList = new IdeaList({el: '#ideaList', button: '#button-ideaList'}).render();

            // Idea panel
            $w.ideaPanel = new IdeaPanel({el: '#ideaPanel', button: '#button-ideaPanel'}).render();

            // Message
            $w.messageList = new MessageList({el: '#messageList', button: '#button-messages'}).render();

            // Synthesis
            $w.syntheses = new Synthesis.Collection();
            var nextSynthesisModel = new Synthesis.Model({'@id': 'next_synthesis'});
            nextSynthesisModel.fetch();

            $w.syntheses.add(nextSynthesisModel);
            $w.synthesisPanel = new SynthesisPanel({
                el: '#synthesisPanel',
                button: '#button-synthesis',
                model: nextSynthesisModel
            });*/

        },

        /**
         * Load the default view
         * */
        home: function(){
            Assembl.headerRegions.show(new navBar());

            if(!window.localStorage.getItem('showNotification')){
               Assembl.notificationRegion.show(new Notification());
            }

            /**
             * Render the current group of view
             * */
            groupManager.getGroupItem();
        },

        idea: function(id){
            //Ctx.openPanel(assembl.ideaList);
            collectionManager.getAllIdeasCollectionPromise().done(
                function(allIdeasCollection) {
                  var idea = allIdeasCollection.get(id);
                  if( idea ){
                    Ctx.setCurrentIdea(idea);
                  }
                });
        },

        /**
         * Alias for `idea`
         */
        ideaSlug: function(slug, id){
            return this.idea(slug +'/'+ id);
        },

        message: function(id){
            //TODO: add new behavior to show messageList Panel
            //Ctx.openPanel(assembl.messageList);
            Assembl.vent.trigger('messageList:showMessageById', id);
        },

        /**
         * Alias for `message`
         */
        messageSlug: function(slug, id){
            return this.message(slug +'/'+ id);
        }

    });

    return new Controller();

});